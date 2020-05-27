# -*- coding: utf-8 -*-
###########################################################################
# Copyright (c), The AiiDA team. All rights reserved.                     #
# This file is part of the AiiDA code.                                    #
#                                                                         #
# The code is hosted on GitHub at https://github.com/aiidateam/aiida-core #
# For further information on the license, see the LICENSE.txt file        #
# For further information please visit http://www.aiida.net               #
###########################################################################
# pylint: disable=no-name-in-module
"""Tests to run with a running daemon."""
import subprocess
import sys
import time

from aiida.common import exceptions
from aiida.engine import run, submit
from aiida.engine.daemon.client import get_daemon_client
from aiida.engine.persistence import ObjectLoader
from aiida.manage.caching import enable_caching
from aiida.orm import CalcJobNode, load_node, Int, Str, List, Dict, load_code
from aiida.plugins import CalculationFactory
from workchains import (
    NestedWorkChain, DynamicNonDbInput, DynamicDbInput, DynamicMixedInput, ListEcho, CalcFunctionRunnerWorkChain,
    WorkFunctionRunnerWorkChain, NestedInputNamespace, SerializeWorkChain, ArithmeticAddBaseWorkChain
)

CODENAME_ADD = 'add@localhost'
CODENAME_DOUBLER = 'doubler'
TIMEOUTSECS = 4 * 60  # 4 minutes
NUMBER_CALCULATIONS = 15  # Number of calculations to submit
NUMBER_WORKCHAINS = 8  # Number of workchains to submit


def print_daemon_log():
    """Print daemon log."""
    daemon_client = get_daemon_client()
    daemon_log = daemon_client.daemon_log_file

    print("Output of 'cat {}':".format(daemon_log))
    try:
        print(subprocess.check_output(
            ['cat', '{}'.format(daemon_log)],
            stderr=subprocess.STDOUT,
        ))
    except subprocess.CalledProcessError as exception:
        print('Note: the command failed, message: {}'.format(exception))


def jobs_have_finished(pks):
    """Check if jobs with given pks have finished."""
    finished_list = [load_node(pk).is_terminated for pk in pks]
    node_list = [load_node(pk) for pk in pks]
    num_finished = len([_ for _ in finished_list if _])

    for node in node_list:
        if not node.is_terminated:
            print('not terminated: {} [{}]'.format(node.pk, node.process_state))
    print('{}/{} finished'.format(num_finished, len(finished_list)))
    return False not in finished_list


def print_report(pk):
    """Print the process report for given pk."""
    print("Output of 'verdi process report {}':".format(pk))
    try:
        print(subprocess.check_output(
            ['verdi', 'process', 'report', '{}'.format(pk)],
            stderr=subprocess.STDOUT,
        ))
    except subprocess.CalledProcessError as exception:
        print('Note: the command failed, message: {}'.format(exception))


def validate_calculations(expected_results):
    """Validate the calculations."""
    valid = True
    actual_dict = {}
    for pk, expected_dict in expected_results.items():
        calc = load_node(pk)
        if not calc.is_finished_ok:
            print(
                'Calculation<{}> not finished ok: process_state<{}> exit_status<{}>'.format(
                    pk, calc.process_state, calc.exit_status
                )
            )
            print_report(pk)
            valid = False

        try:
            actual_dict = calc.outputs.output_parameters.get_dict()
        except exceptions.NotExistent:
            print('Could not retrieve `output_parameters` node for Calculation<{}>'.format(pk))
            print_report(pk)
            valid = False

        try:
            actual_dict['retrieved_temporary_files'] = dict(actual_dict['retrieved_temporary_files'])
        except KeyError:
            # If the retrieval fails we simply pass as the following check of the actual value will fail anyway
            pass

        if actual_dict != expected_dict:
            print('* UNEXPECTED VALUE {} for calc pk={}: I expected {}'.format(actual_dict, pk, expected_dict))
            valid = False

    return valid


def validate_workchains(expected_results):
    """Validate the workchains."""
    valid = True
    for pk, expected_value in expected_results.items():
        this_valid = True
        try:
            calc = load_node(pk)
            actual_value = calc.outputs.output
        except (exceptions.NotExistent, AttributeError) as exception:
            print(
                '* UNABLE TO RETRIEVE VALUE for workchain pk={}: I expected {}, I got {}: {}'.format(
                    pk, expected_value, type(exception), exception
                )
            )
            valid = False
            this_valid = False
            actual_value = None

        # I check only if this_valid, otherwise calc could not exist
        if this_valid and not calc.is_finished_ok:
            print(
                'Calculation<{}> not finished ok: process_state<{}> exit_status<{}>'.format(
                    pk, calc.process_state, calc.exit_status
                )
            )
            print_report(pk)
            valid = False
            this_valid = False

        # I check only if this_valid, otherwise actual_value could be unset
        if this_valid and actual_value != expected_value:
            print(
                '* UNEXPECTED VALUE {}, type {} for workchain pk={}: I expected {}, type {}'.format(
                    actual_value, type(actual_value), pk, expected_value, type(expected_value)
                )
            )
            valid = False
            this_valid = False

    return valid


def validate_cached(cached_calcs):
    """
    Check that the calculations with created with caching are indeed cached.
    """
    valid = True
    for calc in cached_calcs:

        if not calc.is_finished_ok:
            print(
                'Cached calculation<{}> not finished ok: process_state<{}> exit_status<{}>'.format(
                    calc.pk, calc.process_state, calc.exit_status
                )
            )
            print_report(calc.pk)
            valid = False

        if '_aiida_cached_from' not in calc.extras or calc.get_hash() != calc.get_extra('_aiida_hash'):
            print('Cached calculation<{}> has invalid hash'.format(calc.pk))
            print_report(calc.pk)
            valid = False

        if isinstance(calc, CalcJobNode):
            original_calc = load_node(calc.get_extra('_aiida_cached_from'))
            files_original = original_calc.list_object_names()
            files_cached = calc.list_object_names()

            if not files_cached:
                print('Cached calculation <{}> does not have any raw inputs files'.format(calc.pk))
                print_report(calc.pk)
                valid = False
            if not files_original:
                print(
                    'Original calculation <{}> does not have any raw inputs files after being cached from.'.format(
                        original_calc.pk
                    )
                )
                valid = False

            if set(files_original) != set(files_cached):
                print(
                    'different raw input files [{}] vs [{}] for original<{}> and cached<{}> calculation'.format(
                        set(files_original), set(files_cached), original_calc.pk, calc.pk
                    )
                )
                valid = False

    return valid


def launch_calculation(code, counter, inputval):
    """
    Launch calculations to the daemon through the Process layer
    """
    process, inputs, expected_result = create_calculation_process(code=code, inputval=inputval)
    calc = submit(process, **inputs)
    print('[{}] launched calculation {}, pk={}'.format(counter, calc.uuid, calc.pk))
    return calc, expected_result


def run_calculation(code, counter, inputval):
    """
    Run a calculation through the Process layer.
    """
    process, inputs, expected_result = create_calculation_process(code=code, inputval=inputval)
    _, calc = run.get_node(process, **inputs)
    print('[{}] ran calculation {}, pk={}'.format(counter, calc.uuid, calc.pk))
    return calc, expected_result


def create_calculation_process(code, inputval):
    """
    Create the process and inputs for a submitting / running a calculation.
    """
    TemplatereplacerCalculation = CalculationFactory('templatereplacer')
    parameters = Dict(dict={'value': inputval})
    template = Dict(
        dict={
            # The following line adds a significant sleep time.
            # I set it to 1 second to speed up tests
            # I keep it to a non-zero value because I want
            # To test the case when AiiDA finds some calcs
            # in a queued state
            # 'cmdline_params': ["{}".format(counter % 3)], # Sleep time
            'cmdline_params': ['1'],
            'input_file_template': '{value}',  # File just contains the value to double
            'input_file_name': 'value_to_double.txt',
            'output_file_name': 'output.txt',
            'retrieve_temporary_files': ['triple_value.tmp']
        }
    )
    options = {
        'resources': {
            'num_machines': 1
        },
        'max_wallclock_seconds': 5 * 60,
        'withmpi': False,
        'parser_name': 'templatereplacer.doubler',
    }

    expected_result = {'value': 2 * inputval, 'retrieved_temporary_files': {'triple_value.tmp': str(inputval * 3)}}

    inputs = {
        'code': code,
        'parameters': parameters,
        'template': template,
        'metadata': {
            'options': options,
        }
    }
    return TemplatereplacerCalculation, inputs, expected_result


def run_base_restart_workchain():
    """Run the `AddArithmeticBaseWorkChain` a few times for various inputs."""
    code = load_code(CODENAME_ADD)
    inputs = {
        'add': {
            'x': Int(1),
            'y': Int(2),
            'code': code,
            'settings': Dict(dict={'allow_negative': False}),
        }
    }

    # Normal inputs should run just fine
    results, node = run.get_node(ArithmeticAddBaseWorkChain, **inputs)
    assert node.is_finished_ok, node.exit_status
    assert len(node.called) == 1
    assert 'sum' in results
    assert results['sum'].value == 3

    # With one input negative, the sum will be negative which will fail the calculation, but the error handler should
    # fix it, so the second calculation should finish successfully
    inputs['add']['y'] = Int(-4)
    results, node = run.get_node(ArithmeticAddBaseWorkChain, **inputs)
    assert node.is_finished_ok, node.exit_status
    assert len(node.called) == 2
    assert 'sum' in results
    assert results['sum'].value == 5

    # The silly sanity check aborts the workchain if the sum is bigger than 10
    inputs['add']['y'] = Int(10)
    results, node = run.get_node(ArithmeticAddBaseWorkChain, **inputs)
    assert not node.is_finished_ok, node.process_state
    assert node.exit_status == ArithmeticAddBaseWorkChain.exit_codes.ERROR_TOO_BIG.status, node.exit_status  # pylint: disable=no-member
    assert len(node.called) == 1

    # Check that overriding default handler enabled status works
    inputs['add']['y'] = Int(1)
    inputs['handler_overrides'] = Dict(dict={'disabled_handler': True})
    results, node = run.get_node(ArithmeticAddBaseWorkChain, **inputs)
    assert not node.is_finished_ok, node.process_state
    assert node.exit_status == ArithmeticAddBaseWorkChain.exit_codes.ERROR_ENABLED_DOOM.status, node.exit_status  # pylint: disable=no-member
    assert len(node.called) == 1


def main():
    """Launch a bunch of calculation jobs and workchains."""
    # pylint: disable=too-many-locals,too-many-statements
    expected_results_calculations = {}
    expected_results_workchains = {}
    code_doubler = load_code(CODENAME_DOUBLER)

    # Run the `AddArithmeticBaseWorkChain`
    print('Running the `AddArithmeticBaseWorkChain`')
    run_base_restart_workchain()

    # Submitting the Calculations the new way directly through the launchers
    print('Submitting {} calculations to the daemon'.format(NUMBER_CALCULATIONS))
    for counter in range(1, NUMBER_CALCULATIONS + 1):
        inputval = counter
        calc, expected_result = launch_calculation(code=code_doubler, counter=counter, inputval=inputval)
        expected_results_calculations[calc.pk] = expected_result

    # Submitting the Workchains
    print('Submitting {} workchains to the daemon'.format(NUMBER_WORKCHAINS))
    for index in range(NUMBER_WORKCHAINS):
        inp = Int(index)
        _, node = run.get_node(NestedWorkChain, inp=inp)
        expected_results_workchains[node.pk] = index

    print("Submitting a workchain with 'submit'.")
    builder = NestedWorkChain.get_builder()
    input_val = 4
    builder.inp = Int(input_val)
    proc = submit(builder)
    expected_results_workchains[proc.pk] = input_val

    print('Submitting a workchain with a nested input namespace.')
    value = Int(-12)
    pk = submit(NestedInputNamespace, foo={'bar': {'baz': value}}).pk

    print('Submitting a workchain with a dynamic non-db input.')
    value = [4, 2, 3]
    pk = submit(DynamicNonDbInput, namespace={'input': value}).pk
    expected_results_workchains[pk] = value

    print('Submitting a workchain with a dynamic db input.')
    value = 9
    pk = submit(DynamicDbInput, namespace={'input': Int(value)}).pk
    expected_results_workchains[pk] = value

    print('Submitting a workchain with a mixed (db / non-db) dynamic input.')
    value_non_db = 3
    value_db = Int(2)
    pk = submit(DynamicMixedInput, namespace={'inputs': {'input_non_db': value_non_db, 'input_db': value_db}}).pk
    expected_results_workchains[pk] = value_non_db + value_db

    print('Submitting the serializing workchain')
    pk = submit(SerializeWorkChain, test=Int).pk
    expected_results_workchains[pk] = ObjectLoader().identify_object(Int)

    print('Submitting the ListEcho workchain.')
    list_value = List()
    list_value.extend([1, 2, 3])
    pk = submit(ListEcho, list=list_value).pk
    expected_results_workchains[pk] = list_value

    print('Submitting a WorkChain which contains a workfunction.')
    value = Str('workfunction test string')
    pk = submit(WorkFunctionRunnerWorkChain, input=value).pk
    expected_results_workchains[pk] = value

    print('Submitting a WorkChain which contains a calcfunction.')
    value = Int(1)
    pk = submit(CalcFunctionRunnerWorkChain, input=value).pk
    expected_results_workchains[pk] = Int(2)

    calculation_pks = sorted(expected_results_calculations.keys())
    workchains_pks = sorted(expected_results_workchains.keys())
    pks = calculation_pks + workchains_pks

    print('Wating for end of execution...')
    start_time = time.time()
    exited_with_timeout = True
    while time.time() - start_time < TIMEOUTSECS:
        time.sleep(15)  # Wait a few seconds

        # Print some debug info, both for debugging reasons and to avoid
        # that the test machine is shut down because there is no output

        print('#' * 78)
        print('####### TIME ELAPSED: {} s'.format(time.time() - start_time))
        print('#' * 78)
        print("Output of 'verdi process list -a':")
        try:
            print(subprocess.check_output(
                ['verdi', 'process', 'list', '-a'],
                stderr=subprocess.STDOUT,
            ))
        except subprocess.CalledProcessError as exception:
            print('Note: the command failed, message: {}'.format(exception))

        print("Output of 'verdi daemon status':")
        try:
            print(subprocess.check_output(
                ['verdi', 'daemon', 'status'],
                stderr=subprocess.STDOUT,
            ))
        except subprocess.CalledProcessError as exception:
            print('Note: the command failed, message: {}'.format(exception))

        if jobs_have_finished(pks):
            print('Calculation terminated its execution')
            exited_with_timeout = False
            break

    if exited_with_timeout:
        print_daemon_log()
        print('')
        print('Timeout!! Calculation did not complete after {} seconds'.format(TIMEOUTSECS))
        sys.exit(2)
    else:
        # Launch the same calculations but with caching enabled -- these should be FINISHED immediately
        cached_calcs = []
        with enable_caching(identifier='aiida.calculations:templatereplacer'):
            for counter in range(1, NUMBER_CALCULATIONS + 1):
                inputval = counter
                calc, expected_result = run_calculation(code=code_doubler, counter=counter, inputval=inputval)
                cached_calcs.append(calc)
                expected_results_calculations[calc.pk] = expected_result

        if (
            validate_calculations(expected_results_calculations) and
            validate_workchains(expected_results_workchains) and validate_cached(cached_calcs)
        ):
            print_daemon_log()
            print('')
            print('OK, all calculations have the expected parsed result')
            sys.exit(0)
        else:
            print_daemon_log()
            print('')
            print('ERROR! Some return values are different from the expected value')
            sys.exit(3)


if __name__ == '__main__':
    main()
