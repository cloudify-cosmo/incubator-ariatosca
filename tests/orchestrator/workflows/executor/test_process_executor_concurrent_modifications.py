# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import time

import fasteners
import pytest

from aria.orchestrator import events
from aria.orchestrator.workflows.exceptions import ExecutorException
from aria.orchestrator.workflows import api
from aria.orchestrator.workflows.executor import process
from aria.orchestrator import workflow, operation

import tests
from tests.orchestrator.context import execute as execute_workflow
from tests.orchestrator.workflows.helpers import events_collector
from tests import mock
from tests import storage
from tests import helpers


@pytest.fixture
def dataholder(tmpdir):
    dataholder_path = str(tmpdir.join('dataholder'))
    holder = helpers.FilesystemDataHolder(dataholder_path)
    return holder


def test_concurrent_modification_on_task_succeeded(context, executor, lock_files, dataholder):
    _test(context, executor, lock_files, _test_task_succeeded, dataholder, expected_failure=False)


@operation
def _test_task_succeeded(ctx, lock_files, key, first_value, second_value, holder_path):
    _concurrent_update(lock_files, ctx.node, key, first_value, second_value, holder_path)


def test_concurrent_modification_on_task_failed(context, executor, lock_files, dataholder):
    _test(context, executor, lock_files, _test_task_failed, dataholder, expected_failure=True)


@operation
def _test_task_failed(ctx, lock_files, key, first_value, second_value, holder_path):
    first = _concurrent_update(lock_files, ctx.node, key, first_value, second_value, holder_path)
    if not first:
        raise RuntimeError('MESSAGE')


def _test(context, executor, lock_files, func, dataholder, expected_failure):
    def _node(ctx):
        return ctx.model.node.get_by_name(mock.models.DEPENDENCY_NODE_NAME)

    interface_name, operation_name = mock.operations.NODE_OPERATIONS_INSTALL[0]

    key = 'key'
    first_value = 'value1'
    second_value = 'value2'
    arguments = {
        'lock_files': lock_files,
        'key': key,
        'first_value': first_value,
        'second_value': second_value,
        'holder_path': dataholder.path
    }

    node = _node(context)
    interface = mock.models.create_interface(
        node.service,
        interface_name,
        operation_name,
        operation_kwargs=dict(function='{0}.{1}'.format(__name__, func.__name__),
                              arguments=arguments)
    )
    node.interfaces[interface.name] = interface
    context.model.node.update(node)

    @workflow
    def mock_workflow(graph, **_):
        graph.add_tasks(
            api.task.OperationTask(
                node,
                interface_name=interface_name,
                operation_name=operation_name,
                arguments=arguments),
            api.task.OperationTask(
                node,
                interface_name=interface_name,
                operation_name=operation_name,
                arguments=arguments)
        )

    signal = events.on_failure_task_signal
    with events_collector(signal) as collected:
        try:
            execute_workflow(mock_workflow, context, executor)
        except ExecutorException:
            pass

    props = _node(context).attributes
    assert dataholder['invocations'] == 2
    assert props[key].value == dataholder[key]

    exceptions = [event['kwargs']['exception'] for event in collected.get(signal, [])]
    if expected_failure:
        assert exceptions


@pytest.fixture
def executor():
    result = process.ProcessExecutor(python_path=[tests.ROOT_DIR])
    try:
        yield result
    finally:
        result.close()


@pytest.fixture
def context(tmpdir):
    result = mock.context.simple(str(tmpdir))
    yield result
    storage.release_sqlite_storage(result.model)


@pytest.fixture
def lock_files(tmpdir):
    return str(tmpdir.join('first_lock_file')), str(tmpdir.join('second_lock_file'))


def _concurrent_update(lock_files, node, key, first_value, second_value, holder_path):
    holder = helpers.FilesystemDataHolder(holder_path)
    locker1 = fasteners.InterProcessLock(lock_files[0])
    locker2 = fasteners.InterProcessLock(lock_files[1])

    first = locker1.acquire(blocking=False)

    if first:
        # Give chance for both processes to acquire locks
        while locker2.acquire(blocking=False):
            locker2.release()
            time.sleep(0.1)
    else:
        locker2.acquire()

    node.attributes[key] = first_value if first else second_value
    holder['key'] = first_value if first else second_value
    holder.setdefault('invocations', 0)
    holder['invocations'] += 1

    if first:
        locker1.release()
    else:
        with locker1:
            locker2.release()

    return first
