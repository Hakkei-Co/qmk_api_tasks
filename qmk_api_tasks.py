#!/usr/bin/env python3
import threading
from datetime import datetime
from os import environ
from time import sleep, strftime, time
from traceback import print_exc
from wsgiref.simple_server import make_server, WSGIRequestHandler

import qmk_redis
from cleanup_storage import cleanup_storage
from qmk_commands import discord_msg
from qmk_compiler import compile_firmware
from update_kb_redis import update_kb_redis

environ['S3_ACCESS_KEY'] = environ.get('S3_ACCESS_KEY', 'minio_dev')
environ['S3_SECRET_KEY'] = environ.get('S3_SECRET_KEY', 'minio_dev_secret')
COMPILE_TIMEOUT = int(environ.get('COMPILE_TIMEOUT', 600))  # 10 minutes, how long we wait for a specific board to compile
ERROR_LOG_NAG = environ.get('ERROR_LOG_NAG', 'yes') == 'yes'  # When 'yes', send a message to discord about how long the error log is.
ERROR_LOG_URL = environ.get('ERROR_LOG_URL', 'http://api.qmk.fm/v1/keyboards/error_log')  # The URL for the error_log
ERROR_PAGE_URL = environ.get('ERROR_PAGE_URL', 'https://yanfali.github.io/qmk_error_page/')  # The URL to the keyboard status page
JOB_QUEUE_THRESHOLD = int(environ.get('JOB_QUEUE_THRESHOLD', 1))  # When there are more than this many jobs in the queue we don't compile anything
JOB_QUEUE_WAIT = int(environ.get('JOB_QUEUE_WAIT', 300))  # 5 Minutes, How long to wait when the job queue is too large
JOB_QUEUE_TOO_LONG = int(environ.get('JOB_QUEUE_TOO_LONG', 1800))  # 30 Minutes, How long it's been since the last compile before we warn it's been too long
MSG_ON_GOOD_COMPILE = environ.get('MSG_ON_GOOD_COMPILE', 'yes') == 'yes'  # When 'yes', send a message to discord for good compiles
MSG_ON_BAD_COMPILE = environ.get('MSG_ON_BAD_COMPILE', 'yes') == 'yes'  # When 'yes', send a message to discord for failed compiles
MSG_ON_LOOP_COMPLETION = environ.get('MSG_ON_LOOP_COMPLETION', 'yes') == 'yes'  # When 'yes', send a message to discord summarizing how many keyboards work and don't work.
MSG_ON_S3_SUCCESS = environ.get('MSG_ON_S3_SUCCESS', 'yes') == 'yes'  # When 'yes', send a message to discord for successful S3 cleanup
MSG_ON_S3_FAIL = environ.get('MSG_ON_S3_FAIL', 'yes') == 'yes'  # When 'yes', send a message to discord for failed S3 cleanup
MSG_ON_QMK_FAIL = environ.get('MSG_ON_QMK_FAIL', 'yes') == 'yes'  # When 'yes', send a message to discord for failed QMK updates
S3_CLEANUP_TIMEOUT = int(environ.get('S3_CLEANUP_TIMEOUT', 300))  # 5 Minutes, how long we wait for the S3 cleanup process to run
S3_CLEANUP_PERIOD = int(environ.get('S3_CLEANUP_PERIOD', 900))  # 15 Minutes, how often S3 is cleaned up
QMK_UPDATE_TIMEOUT = int(environ.get('QMK_UPDATE_TIMEOUT', 600))  # 10 Minutes, how long we wait for the qmk_firmware update to run
QUEUE_TIMEOUT = int(environ.get('QUEUE_TIMEOUT', 3600))  # 1 Hour, how long we wait for qmk_firmware_update and s3_cleanup to queue
BUILD_STATUS_TIMEOUT = int(environ.get('BUILD_STATUS_TIMEOUT', 86400 * 7))  # 1 week, how old configurator_build_status entries should be to get removed
TIME_FORMAT = environ.get('TIME_FORMAT', '%Y-%m-%d %H:%M:%S %z')

# Status tracking variables
job_queue_last_compile = datetime.now()
job_queue_last_warning = time()
last_s3_cleanup = 0

# Simple WSGI app to give Rancher a healthcheck to hit
port = 5000
status = {
    'good': ['200 OK', '¡Bueno!\n'],
    'bad': ['500 Internal Server Error', '¡Muy mal!\n'],
    'current': 'bad',
}


class NoLoggingWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def current_status(i):
    """Return the current status.
    """
    return status[status['current']][i]


def wsgi_app(environ, start_response):
    start_response(current_status(0), [('Content-Type', 'text/plain')])
    return [current_status(1).encode('UTF-8')]


def find_my_queue_position(job_id):
    """Returns the number of jobs ahead of you in the queue.
    """
    for i, job in enumerate(qmk_redis.rq.jobs):
        if job.id == job_id:
            return i


def wait_for_job_start(job, timeout=QUEUE_TIMEOUT):
    """Waits until the indicated job has started, then returns.
    """
    start_time = time()

    while job.get_status() in ['queued', 'deferred']:
        if time() - start_time > timeout:
            return False
        sleep(1)

    return True


def periodic_tasks():
    """Jobs that need to run on a regular schedule.
    """
    qmk_redis.set('qmk_api_tasks_ping', time())
    s3_cleanup()
    update_qmk_firmware()


def s3_cleanup():
    """Clean up old compile jobs on S3.
    """
    global last_s3_cleanup

    if time() - last_s3_cleanup > S3_CLEANUP_PERIOD:
        print('***', strftime(TIME_FORMAT))
        print('Beginning S3 storage cleanup.')
        job = qmk_redis.enqueue(cleanup_storage, timeout=S3_CLEANUP_TIMEOUT)
        print('Successfully enqueued job id %s at %s, polling every 2 seconds...' % (job.id, strftime(TIME_FORMAT)))
        start_time = time()
        run_time = None

        # Wait for the job to start running
        if not wait_for_job_start(job):
            print('S3 cleanup queued for %s seconds! Giving up at %s!' % (QUEUE_TIMEOUT, strftime(TIME_FORMAT)))
            if MSG_ON_S3_FAIL:
                discord_msg('warning', 'S3 cleanup queue longer than %s seconds! Queue length %s!' % (QUEUE_TIMEOUT, len(qmk_redis.rq.jobs)))
            return False

        # Monitor the job while it runs
        while job.get_status() == 'started':
            if time() - start_time > S3_CLEANUP_TIMEOUT + 5:
                print('S3 cleanup took longer than %s seconds! Cancelling at %s!' % (S3_CLEANUP_TIMEOUT, strftime(TIME_FORMAT)))
                if MSG_ON_S3_FAIL:
                    discord_msg('warning', 'S3 cleanup took longer than %s seconds!' % (S3_CLEANUP_TIMEOUT,))
                break
            sleep(2)

        # Check over the S3 cleanup results
        if job.result:
            print('Cleanup job completed successfully!')
            if MSG_ON_S3_SUCCESS:
                discord_msg('info', 'S3 cleanup completed successfully.')
        else:
            print('Could not clean S3!')
            print(job)
            print(job.result)
            if MSG_ON_S3_FAIL:
                discord_msg('error', 'S3 cleanup did not complete successfully!')

        last_s3_cleanup = time()


def update_qmk_firmware():
    """Update the API from qmk_firmware after a push.
    """
    if qmk_redis.get('qmk_needs_update'):
        print('***', strftime(TIME_FORMAT))
        print('Beginning qmk_firmware update.')
        job = qmk_redis.enqueue(update_kb_redis, timeout=QMK_UPDATE_TIMEOUT)
        print('Successfully enqueued job id %s at %s, polling every 2 seconds...' % (job.id, strftime(TIME_FORMAT)))
        start_time = time()

        # Wait for the job to start running
        if not wait_for_job_start(job):
            print('QMK update queued for %s seconds! Giving up at %s!' % (S3_CLEANUP_TIMEOUT, strftime(TIME_FORMAT)))
            if MSG_ON_QMK_FAIL:
                discord_msg('warning', 'S3 cleanup queue longer than %s seconds! Queue length %s!' % (S3_CLEANUP_TIMEOUT, len(qmk_redis.rq.jobs)))
            return False

        # Monitor the job while it runs
        while job.get_status() == 'started':
            if time() - start_time > QMK_UPDATE_TIMEOUT + 5:
                print('QMK update took longer than %s seconds! Cancelling at %s!' % (QMK_UPDATE_TIMEOUT, strftime(TIME_FORMAT)))
                if MSG_ON_QMK_FAIL:
                    discord_msg('error', 'QMK update took longer than %s seconds!' % (QMK_UPDATE_TIMEOUT,))
                break
            sleep(2)

        # Check over the results
        if job.result:
            hash = qmk_redis.get('qmk_api_last_updated')['git_hash']
            message = 'QMK update completed successfully! API is current to ' + hash
            print(message)
            discord_msg('info', message)

            if ERROR_LOG_NAG:
                error_log = qmk_redis.get('qmk_api_update_error_log')
                if len(error_log) > 5:
                    discord_msg('error', 'There are %s errors from the update process. View them here: %s' % (len(error_log), ERROR_LOG_URL))
        else:
            print('Could not update qmk_firmware!')
            print(job)
            print(job.result)
            if MSG_ON_QMK_FAIL:
                discord_msg('warning', 'QMK update did not complete successfully!')


class WebThread(threading.Thread):
    def run(self):
        httpd = make_server('', port, wsgi_app, handler_class=NoLoggingWSGIRequestHandler)
        httpd.serve_forever()


# The main part of the app, iterate over all keyboards and build them.
class TaskThread(threading.Thread):
    def run(self):
        status['current'] = 'good'
        last_compile = 0
        last_good_boards = qmk_redis.get('qmk_last_good_boards')
        last_bad_boards = qmk_redis.get('qmk_last_bad_boards')
        last_stop = qmk_redis.get('qmk_api_tasks_current_keyboard')

        keyboards_tested = qmk_redis.get('qmk_api_keyboards_tested')  # FIXME: Remove when no longer used
        if not keyboards_tested:
            keyboards_tested = {}

        failed_keyboards = qmk_redis.get('qmk_api_keyboards_failed')  # FIXME: Remove when no longer used
        if not failed_keyboards:
            failed_keyboards = {}

        configurator_build_status = qmk_redis.get('qmk_api_configurator_status')
        if not configurator_build_status:
            configurator_build_status = {}

        while True:
            good_boards = 0
            bad_boards = 0
            keyboard_list = qmk_redis.get('qmk_api_keyboards')

            # If we stopped at a known keyboard restart from there
            if last_stop in keyboard_list:
                good_boards = qmk_redis.get('qmk_good_boards') or 0
                bad_boards = qmk_redis.get('qmk_bad_boards') or 0
                del(keyboard_list[:keyboard_list.index(last_stop)])
                last_stop = None

            for keyboard in keyboard_list:
                global job_queue_last_compile
                global job_queue_last_warning

                periodic_tasks()

                # Cycle through each keyboard and build it
                try:
                    # If we have too many jobs in the queue don't put stress on the infrastructure
                    while len(qmk_redis.rq.jobs) > JOB_QUEUE_THRESHOLD:
                        periodic_tasks()
                        print('Too many jobs in the redis queue (%s)! Sleeping %s seconds...' % (len(qmk_redis.rq.jobs), COMPILE_TIMEOUT))
                        sleep(COMPILE_TIMEOUT)

                        if time() - job_queue_last_warning > JOB_QUEUE_TOO_LONG:
                            job_queue_last_warning = time()
                            level = 'warning'
                            message = 'Compile queue too large (%s) since %s' % (len(qmk_redis.rq.jobs), job_queue_last_compile.isoformat())
                            discord_msg(level, message)

                    # Cycle through each layout for this keyboard and build it
                    qmk_redis.set('qmk_api_tasks_current_keyboard', keyboard)
                    layout_results = {}
                    metadata = qmk_redis.get('qmk_api_kb_%s' % (keyboard))
                    if not metadata['layouts']:
                        bad_boards += 1
                        qmk_redis.set('qmk_bad_boards', bad_boards)
                        keyboards_tested[keyboard + '/[NO_LAYOUTS]'] = False  # FIXME: Remove when no longer used
                        failed_keyboards[keyboard + '/[NO_LAYOUTS]'] = {'severity': 'error', 'message': '%s: No layouts defined.' % keyboard}  # FIXME: Remove when no longer used
                        configurator_build_status[keyboard + '/[NO_LAYOUTS]'] = {'works': False, 'last_tested': int(time()), 'message': '%s: No Layouts defined.' % keyboard}
                        continue

                    for layout_macro in list(metadata['layouts']):
                        # Skip KEYMAP as it's deprecated
                        if 'KEYMAP' in layout_macro:
                            continue

                        # Build a layout
                        keyboard_layout_name = '/'.join((keyboard, layout_macro))
                        layers = [list(map(lambda x: 'KC_NO', metadata['layouts'][layout_macro]['layout']))]
                        layers.append(list(map(lambda x: 'KC_TRNS', layers[0])))

                        # Enqueue the job
                        print('***', strftime(TIME_FORMAT))
                        print('Beginning test compile for %s, layout %s' % (keyboard, layout_macro))
                        args = (keyboard, 'qmk_api_tasks_test_compile', layout_macro, layers)
                        job = qmk_redis.enqueue(compile_firmware, COMPILE_TIMEOUT, *args)
                        print('Successfully enqueued, polling every 2 seconds...')

                        # Wait for the job to start running
                        if not wait_for_job_start(job):
                            print('Waited %s seconds for %s/%s to start! Giving up at %s!' % (S3_CLEANUP_TIMEOUT, keyboard, layout_macro, strftime(TIME_FORMAT)))
                            if MSG_ON_BAD_COMPILE:
                                discord_msg('warning', 'Keyboard %s/%s waited in queue longer than %s seconds! Queue length %s!' % (keyboard, layout_macro, S3_CLEANUP_TIMEOUT, len(qmk_redis.rq.jobs)))

                        else:
                            timeout = time() + COMPILE_TIMEOUT + 5
                            while job.get_status() == 'started':
                                if time() > timeout:
                                    print('Compile timeout reached after %s seconds, giving up on this job.' % (COMPILE_TIMEOUT))
                                    layout_results[keyboard_layout_name] = {'result': False, 'reason': '**%s**: Compile timeout reached.' % layout_macro}
                                    break
                                sleep(2)

                        # Check over the job results
                        if job.result and job.result['returncode'] == 0:
                            print('Compile job completed successfully!')
                            good_boards += 1
                            qmk_redis.set('qmk_good_boards', good_boards)
                            configurator_build_status[keyboard_layout_name] = {'works': True, 'last_tested': int(time()), 'message': job.result['output']}
                            keyboards_tested[keyboard_layout_name] = True  # FIXME: Remove this when it's no longer used
                            if keyboard_layout_name in failed_keyboards:
                                del failed_keyboards[keyboard_layout_name]  # FIXME: Remove this when it's no longer used
                            layout_results[keyboard_layout_name] = {'result': True, 'reason': layout_macro + ' works in configurator.'}
                        else:
                            if job.result:
                                output = job.result['output']
                                print('Could not compile %s, layout %s, return code %s' % (keyboard, layout_macro, job.result['returncode']))
                                print(output)
                                layout_results[keyboard_layout_name] = {'result': False, 'reason': '**%s** does not work in configurator.' % layout_macro}
                            else:
                                output = 'Job took longer than %s seconds, giving up!' % COMPILE_TIMEOUT
                                layout_results[keyboard_layout_name] = {'result': False, 'reason': '**%s**: Compile timeout reached.' % layout_macro}

                            bad_boards += 1
                            qmk_redis.set('qmk_bad_boards', bad_boards)
                            configurator_build_status[keyboard_layout_name] = {'works': False, 'last_tested': int(time()), 'message': output}
                            keyboards_tested[keyboard_layout_name] = False  # FIXME: Remove this when it's no longer used
                            failed_keyboards[keyboard_layout_name] = {'severity': 'error', 'message': output}  # FIXME: Remove this when it's no longer used

                        # Write our current progress to redis
                        qmk_redis.set('qmk_api_configurator_status', configurator_build_status)
                        qmk_redis.set('qmk_api_keyboards_tested', keyboards_tested)
                        qmk_redis.set('qmk_api_keyboards_failed', failed_keyboards)

                    # Report this keyboard to discord
                    failed_layout = False
                    for result in layout_results.values():
                        if not result['result']:
                            failed_layout = True
                            break

                    if (MSG_ON_GOOD_COMPILE and not failed_layout) or (MSG_ON_BAD_COMPILE and failed_layout):
                        level = 'warning' if failed_layout else 'info'
                        message = 'Configurator summary for **' + keyboard + ':**'
                        for layout, result in layout_results.items():
                            icon = ':green_heart:' if result['result'] else ':broken_heart:'
                            message += '\n%s %s' % (icon, result['reason'])
                        discord_msg(level, message, False)

                except Exception as e:
                    print('***', strftime(TIME_FORMAT))
                    print('Uncaught exception!', e.__class__.__name__)
                    print(e)
                    discord_msg('warning', 'Uncaught exception while testing %s.' % (keyboard,))
                    print_exc()


            # Remove stale build status entries
            print('***', strftime(TIME_FORMAT))
            print('Checking configurator_build_status for stale entries.')
            for keyboard_layout_name in list(configurator_build_status):
                if time() - configurator_build_status[keyboard_layout_name]['last_tested'] > BUILD_STATUS_TIMEOUT:
                    print('Removing stale entry %s because it is %s seconds old' % (keyboard_layout_name, configurator_build_status[keyboard_layout_name]['last_tested']))
                    del configurator_build_status[keyboard_layout_name]

            # Notify discord that we've completed a circuit of the keyboards
            if MSG_ON_LOOP_COMPLETION:
                if last_good_boards is not None:
                    good_difference = good_boards - last_good_boards
                    bad_difference = bad_boards - last_bad_boards

                    if good_difference < 0:
                        good_difference = '%s fewer than' % (good_difference * -1,)
                    elif good_difference > 0:
                        good_difference = '%s more than' % (good_difference,)
                    else:
                        good_difference = 'No change from'

                    if bad_difference < 0:
                        bad_difference = '%s fewer than' % (bad_difference * -1,)
                    elif bad_difference > 0:
                        bad_difference = '%s more than' % (bad_difference,)
                    else:
                        bad_difference = 'No change from'

                    last_good_boards = good_boards
                    last_bad_boards = bad_boards
                    qmk_redis.set('qmk_last_good_boards', good_boards)
                    qmk_redis.set('qmk_last_bad_boards', bad_boards)

                    message = """We've completed a round of testing!

Working: %s the last round, for a total of %s working keyboard/layout combinations.

Non-working: %s the last round, for a total of %s non-working keyboard/layout combinations.

Check out the details here: <%s>"""
                    discord_msg('info', message % (good_difference, good_boards, bad_difference, bad_boards, ERROR_PAGE_URL))

                else:
                    last_good_boards = good_boards
                    last_bad_boards = bad_boards

        # This comes after our `while True:` above and it should not be possible to break out of that loop.
        print('How did we get here this should be impossible! HELP! HELP! HELP!')
        discord_msg('error', 'How did we get here this should impossible! @skullydazed HELP! @skullydazed HELP! @skullydazed HELP!')
        status['current'] = 'bad'


if __name__ == '__main__':
    w = WebThread()
    t = TaskThread()
    w.start()
    t.start()
