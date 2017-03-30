import os
import signal
import traceback
import yaml
import copy
import functools

import lib
from lib.test_suite import TestSuite


from lib.colorer import Colorer
color_stdout = Colorer()


def color_log(*args, **kwargs):
    kwargs['log_only'] = True
    color_stdout(*args, **kwargs)


# Utils
#######


def find_suites():
    suite_names = lib.Options().args.suites
    if suite_names == []:
        for root, dirs, names in os.walk(os.getcwd(), followlinks=True):
            if "suite.ini" in names:
                suite_names.append(os.path.basename(root))

    suites = [TestSuite(suite_name, lib.Options().args)
              for suite_name in sorted(suite_names)]
    return suites


def parse_reproduce_file(filepath):
    reproduce = []
    if not filepath:
        return reproduce
    try:
        with open(filepath, 'r') as f:
            for task_id in yaml.load(f):
                task_name, task_conf = task_id
                reproduce.append((task_name, task_conf))
    except IOError:
        color_stdout('Cannot read "%s" passed as --reproduce argument\n' %
                     filepath, schema='error')
        exit(1)
    return reproduce


# Get tasks and worker generators
#################################


def task_buckets():
    suites = find_suites()
    res = {}
    for suite in suites:
        key = os.path.basename(suite.suite_path)
        gen_worker = functools.partial(Worker, suite)  # get _id as an arg
        task_ids = [task.id for task in suite.find_tests()]
        if task_ids:
            res[key] = {
                'gen_worker': gen_worker,
                'task_ids': task_ids,
            }
    return res


def reproduce_buckets(all_buckets):
    # check test list and find a bucket
    found_bucket_ids = []
    reproduce = parse_reproduce_file(lib.Options().args.reproduce)
    if not reproduce:
        raise ValueError('[reproduce] Tests list cannot be empty')
    for i, task_id in enumerate(reproduce):
        for bucket_id, bucket in all_buckets.items():
            if task_id in bucket['task_ids']:
                found_bucket_ids.append(bucket_id)
                break
        if len(found_bucket_ids) != i + 1:
            raise ValueError('[reproduce] Cannot find test "%s"' %
                             str(task_id))
    found_bucket_ids = list(set(found_bucket_ids))
    if len(found_bucket_ids) < 1:
        raise ValueError('[reproduce] Cannot find any suite for given tests')
    elif len(found_bucket_ids) > 1:
        raise ValueError(
            '[reproduce] Given tests contained by different suites')

    key = found_bucket_ids[0]
    bucket = copy.deepcopy(all_buckets[key])
    bucket['task_ids'] = reproduce
    return {key: bucket}


# Worker results
################


class BaseWorkerResult(object):
    def __init__(self, worker_id, worker_name):
        super(BaseWorkerResult, self).__init__()
        self.worker_id = worker_id
        self.worker_name = worker_name


class TaskResult(BaseWorkerResult):
    def __init__(self, worker_id, worker_name, task_id, short_status):
        super(TaskResult, self).__init__(worker_id, worker_name)
        self.short_status = short_status
        self.task_id = task_id


class WorkerOutput(BaseWorkerResult):
    def __init__(self, worker_id, worker_name, output, log_only):
        super(WorkerOutput, self).__init__(worker_id, worker_name)
        self.output = output
        self.log_only = log_only


class WorkerDone(BaseWorkerResult):
    def __init__(self, worker_id, worker_name):
        super(WorkerDone, self).__init__(worker_id, worker_name)


# Worker
########


class VoluntaryStopException(Exception):
    pass


class Worker:
    def report_keyboard_interrupt(self):
        color_stdout('\n[Worker "%s"] Caught keyboard interrupt; stopping...\n'
                     % self.name, schema='test_var')

    def wrap_output(self, output, log_only):
        return WorkerOutput(self.id, self.name, output, log_only)

    def done_marker(self):
        return WorkerDone(self.id, self.name)

    def wrap_result(self, task_id, short_status):
        return TaskResult(self.id, self.name, task_id, short_status)

    def sigterm_handler(self, signum, frame):
        self.sigterm_received = True

    def __init__(self, suite, _id):
        self.sigterm_received = False
        signal.signal(signal.SIGTERM, lambda x, y, z=self:
                      z.sigterm_handler(x, y))

        self.initialized = False
        self.id = _id
        self.suite = suite
        self.name = '%02d_%s' % (self.id, self.suite.suite_path)

        main_vardir = self.suite.ini['vardir']
        self.suite.ini['vardir'] = os.path.join(main_vardir, self.name)

        reproduce_dir = os.path.join(main_vardir, 'reproduce')
        if not os.path.isdir(reproduce_dir):
            # try-except to prevent races btw workers
            try:
                os.makedirs(reproduce_dir)
            except OSError:
                pass
        self.tests_file = os.path.join(
            reproduce_dir, '%s.list.yaml' % self.name)

        color_stdout.queue_msg_wrapper = self.wrap_output

        self.last_task_done = True
        self.last_task_id = -1

        try:
            self.server = suite.gen_server()
            self.inspector = suite.start_server(self.server)
            self.initialized = True
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
        except Exception as e:
            color_stdout('Worker "%s" cannot start tarantool server; '
                         'the tasks will be ignored...\n' % self.name,
                         schema='error')
            color_stdout("The raised exception is '%s' of type '%s'.\n"
                         % (str(e), str(type(e))), schema='error')
            color_stdout('Worker "%s" received the following error:\n'
                         % self.name + traceback.format_exc() + '\n',
                         schema='error')


    # TODO: What if KeyboardInterrupt raised inside task_queue.get() and 'stop
    #       worker' marker readed from the queue, but not returned to us?
    def task_get(self, task_queue):
        self.last_task_done = False
        self.last_task_id = task_queue.get()
        return self.last_task_id

    @staticmethod
    def is_joinable(task_queue):
        return 'task_done' in task_queue.__dict__.keys()

    def task_done(self, task_queue):
        if Worker.is_joinable(task_queue):
            task_queue.task_done()
        self.last_task_done = True

    def find_task(self, task_id):
        for cur_task in self.suite.tests:
            if cur_task.id == task_id:
                return cur_task
        raise ValueError('Cannot find test: %s' % str(task_id))

    # Note: it's not exception safe
    def run_task(self, task_id):
        if not self.initialized:
            return self.done_marker()
        try:
            task = self.find_task(task_id)
            with open(self.tests_file, 'a') as f:
                f.write('- ' + yaml.safe_dump(task.id))
            short_status = self.suite.run_test(
                task, self.server, self.inspector)
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
            raise
        except Exception as e:
            color_stdout(
                'Worker "%s" received the following error; stopping...\n'
                % self.name + traceback.format_exc() + '\n', schema='error')
            raise
        return short_status

    def run_loop(self, task_queue, result_queue):
        """ called from 'run_all' """
        while True:
            task_id = self.task_get(task_queue)
            # None is 'stop worker' marker
            if task_id is None:
                color_log('Worker "%s" exhausted task queue; '
                          'stopping the server...\n' % self.name,
                          schema='test_var')
                self.stop(task_queue, result_queue)
                break
            short_status = self.run_task(task_id)
            result_queue.put(self.wrap_result(task_id, short_status))
            if not lib.Options().args.is_force and short_status == 'fail':
                color_stdout(
                    'Worker "%s" got failed test; stopping the server...\n'
                    % self.name, schema='test_var')
                raise VoluntaryStopException()
            if self.sigterm_received:
                color_stdout('Worker "%s" got signal to terminate; '
                             'stopping the server...\n' % self.name,
                             schema='test_var')
                raise VoluntaryStopException()
            self.task_done(task_queue)

    def run_all(self, task_queue, result_queue):
        if not self.initialized:
            self.flush_all_tasks(task_queue, result_queue)
            result_queue.put(self.done_marker())
            return

        try:
            self.run_loop(task_queue, result_queue)
        except (KeyboardInterrupt, Exception):
            self.stop(task_queue, result_queue)

        result_queue.put(self.done_marker())

    def stop(self, task_queue, result_queue):
        try:
            if not self.last_task_done:
                self.task_done(task_queue)
            self.flush_all_tasks(task_queue, result_queue)
            self.suite.stop_server(self.server, self.inspector, silent=True)
        except (KeyboardInterrupt, Exception):
            pass

    def flush_all_tasks(self, task_queue, result_queue):
        """ A queue flusing is necessary only for joinable queue (when runner
            controlling workers with using join() on task queues), but for
            unification in reporting 'not_run' status it make sense to leave it
            enabled for any queue type.
        """
        # None is 'stop worker' marker
        while self.last_task_id:
            task_id = self.task_get(task_queue)
            result_queue.put(self.wrap_result(task_id, 'not_run'))
            self.task_done(task_queue)
