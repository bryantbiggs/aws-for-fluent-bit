import os
import sys
import json
import time
import boto3
import math
import subprocess
import validation_bar
from datetime import datetime, timezone
import create_testing_resources.kinesis_s3_firehose.resource_resolver as resource_resolver

WAITER_SLEEP = 30
MAX_WAITER_ATTEMPTS = 240
MAX_WAITER_DESCRIBE_FAILURES = 2
IS_TASK_DEFINITION_PRINTED = True
PLATFORM = os.environ['PLATFORM'].lower()
OUTPUT_PLUGIN = os.environ['OUTPUT_PLUGIN'].lower()
LOG_GROUP_NAME = os.environ.get('CW_LOG_GROUP_NAME', "unavailable")
AWS_REGION = os.environ['AWS_REGION']
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', "unavailable")
TESTING_RESOURCES_STACK_NAME = os.environ['TESTING_RESOURCES_STACK_NAME']
PREFIX = os.environ['PREFIX']
EKS_CLUSTER_NAME = os.environ['EKS_CLUSTER_NAME']
LOGGER_RUN_TIME_IN_SECOND = 600
NUM_OF_EKS_NODES = 4
BUFFER_TIME_IN_SECOND = 600
if OUTPUT_PLUGIN == 'cloudwatch':
    THROUGHPUT_LIST = json.loads(os.environ['CW_THROUGHPUT_LIST'])
    # Cloudwatch requires more waiting for all log events to show up in the stream.
    BUFFER_TIME_IN_SECOND = 2400
else:
    THROUGHPUT_LIST = json.loads(os.environ['THROUGHPUT_LIST'])

# Input Logger Data
INPUT_LOGGERS = [
    {
        "name": "stdstream",
        "logger_image": os.getenv('ECS_APP_IMAGE'), # STDOUT Logs
        "fluent_config_file_path": "./load_tests/logger/stdout_logger/fluent.conf",
        "log_configuration_path": "./load_tests/logger/stdout_logger/log_configuration"
    },
    {
        "name": "tcp",
        "logger_image": os.getenv('ECS_APP_IMAGE_TCP'), # TCP Logs Java App
        "fluent_config_file_path": "./load_tests/logger/tcp_logger/fluent.conf",
        "log_configuration_path": "./load_tests/logger/tcp_logger/log_configuration"
    },
]

PLUGIN_NAME_MAPS = {
    "kinesis": "kinesis_streams",
    "firehose": "kinesis_firehose",
    "s3": "s3",
    "cloudwatch": "cloudwatch_logs",
}

def __sleep(duration, reason):
    print("Sleeping for {}s, ts=[{}] reason=[{}]".format(duration, datetime.now().isoformat(), reason), flush=True)
    time.sleep(duration)

# Return the approximate log delay for each ecs load test
# Estimate log delay = task_stop_time - task_start_time - logger_image_run_time
def get_log_delay(log_delay_epoch_time):
    return datetime.fromtimestamp(log_delay_epoch_time).strftime('%Mm%Ss')

# Set buffer for waiting all logs sent to destinations (~5min)
def set_buffer(stop_epoch_time):
    curr_epoch_time = time.time()
    if (curr_epoch_time - stop_epoch_time) < BUFFER_TIME_IN_SECOND:
        __sleep(int(BUFFER_TIME_IN_SECOND - curr_epoch_time + stop_epoch_time),
            "Waiting for all logs to be sent to destination. "+
            f"destination={OUTPUT_PLUGIN} logGroupName={LOG_GROUP_NAME} s3Bucket={S3_BUCKET_NAME} prefix={PREFIX}")

# convert datetime to epoch time
def parse_time(time):
    return (time - datetime(1970,1,1, tzinfo=timezone.utc)).total_seconds()

# Check app container exit status for each ecs load test
# to make sure it generate correct number of logs
def check_app_exit_code(response):
    containers = response['tasks'][0]['containers']
    if len(containers) < 2:
        sys.exit('[TEST_FAILURE] Error occured to get task container list')
    for container in containers:
        if container['name'] == 'app' and container['exitCode'] != 0:
            print('[TEST_FAILURE] Logger failed to generate all logs with exit code: ' + str(container['exitCode']))
            sys.exit('[TEST_FAILURE] Logger failed to generate all logs with exit code: ' + str(container['exitCode']))

# Return the total number of input records for each load test
def calculate_total_input_number(throughput):
    iteration_per_second = int(throughput[0:-1])*1000
    return str(iteration_per_second * LOGGER_RUN_TIME_IN_SECOND)

# 1. Configure task definition for each load test based on existing templates
# 2. Register generated task definition
def generate_task_definition(throughput, input_logger, s3_fluent_config_arn):
    # Generate configuration information for STD and TCP tests
    std_config      = resource_resolver.get_input_configuration(PLATFORM, resource_resolver.STD_INPUT_PREFIX, throughput)
    custom_config   = resource_resolver.get_input_configuration(PLATFORM, resource_resolver.CUSTOM_INPUT_PREFIX, throughput)

    task_definition_dict = {

        # App Container Environment Variables
        '$APP_IMAGE': input_logger['logger_image'],
        '$LOGGER_RUN_TIME_IN_SECOND': str(LOGGER_RUN_TIME_IN_SECOND),

        # Firelens Container Environment Variables
        '$FLUENT_BIT_IMAGE': os.environ['FLUENT_BIT_IMAGE'],
        '$INPUT_NAME': input_logger['name'],
        '$LOGGER_PORT': "4560",
        '$FLUENT_CONFIG_S3_FILE_ARN': s3_fluent_config_arn,
        '$OUTPUT_PLUGIN': OUTPUT_PLUGIN,

        # General Environment Variables
        '$THROUGHPUT': throughput,

        # Task Environment Variables
        '$TASK_ROLE_ARN': os.environ['LOAD_TEST_TASK_ROLE_ARN'],
        '$TASK_EXECUTION_ROLE_ARN': os.environ['LOAD_TEST_TASK_EXECUTION_ROLE_ARN'],
        '$CUSTOM_S3_OBJECT_NAME':           resource_resolver.resolve_s3_object_name(custom_config),

        # Plugin Specific Environment Variables
        'cloudwatch': {
            '$CW_LOG_GROUP_NAME':               LOG_GROUP_NAME,
            '$STD_LOG_STREAM_NAME':             resource_resolver.resolve_cloudwatch_logs_stream_name(std_config),
            '$CUSTOM_LOG_STREAM_NAME':          resource_resolver.resolve_cloudwatch_logs_stream_name(custom_config)
        },
        'firehose': {
            '$STD_DELIVERY_STREAM_PREFIX':      resource_resolver.resolve_firehose_delivery_stream_name(std_config),
            '$CUSTOM_DELIVERY_STREAM_PREFIX':   resource_resolver.resolve_firehose_delivery_stream_name(custom_config),
        },
        'kinesis': {
            '$STD_STREAM_PREFIX':               resource_resolver.resolve_kinesis_delivery_stream_name(std_config),
            '$CUSTOM_STREAM_PREFIX':            resource_resolver.resolve_kinesis_delivery_stream_name(custom_config),
        },
        's3': {
            '$S3_BUCKET_NAME':                  S3_BUCKET_NAME,
            '$STD_S3_OBJECT_NAME':              resource_resolver.resolve_s3_object_name(std_config),
        },
    }

    # Add log configuration to dictionary
    log_configuration_data = open(f'{input_logger["log_configuration_path"]}/{OUTPUT_PLUGIN}.json', 'r')
    log_configuration_raw = log_configuration_data.read()
    log_configuration = parse_json_template(log_configuration_raw, task_definition_dict)
    task_definition_dict["$LOG_CONFIGURATION"] = log_configuration

    # Parse task definition template
    fin = open(f'./load_tests/task_definitions/{OUTPUT_PLUGIN}.json', 'r')
    data = fin.read()
    task_def_formatted = parse_json_template(data, task_definition_dict)

    # Register task definition
    task_def = json.loads(task_def_formatted)

    if IS_TASK_DEFINITION_PRINTED:
        print("Registering task definition:", flush=True)
        print(json.dumps(task_def, indent=4), flush=True)
        client = boto3.client('ecs')
        client.register_task_definition(
            **task_def
        )
    else:
        print("Registering task definition", flush=True)

# With multiple codebuild projects running parallel,
# Testing resources only needs to be created once
def create_testing_resources():
    session = get_sts_boto_session()

    if OUTPUT_PLUGIN != 'cloudwatch':
        client = session.client('cloudformation')
        waiter = client.get_waiter('stack_exists')
        waiter.wait(
            StackName=TESTING_RESOURCES_STACK_NAME,
            WaiterConfig={
                'MaxAttempts': 60
            }
        )
        waiter = client.get_waiter('stack_create_complete')
        waiter.wait(
            StackName=TESTING_RESOURCES_STACK_NAME
        )
    else:
        # scale up eks cluster
        if PLATFORM == 'eks':
            os.system(f'eksctl scale nodegroup --cluster={EKS_CLUSTER_NAME} --nodes={NUM_OF_EKS_NODES} ng')
            while True:
                __sleep(90, "Waiting for EKS cluster nodes")
                number_of_nodes = subprocess.getoutput("kubectl get nodes --no-headers=true | wc -l")
                if(int(number_of_nodes) == NUM_OF_EKS_NODES):
                    break
            # create namespace
            os.system('kubectl apply -f ./load_tests/create_testing_resources/eks/namespace.yaml')
        # Once deployment starts, it will wait until the stack creation is completed
        os.chdir(f'./load_tests/{sys.argv[1]}/{PLATFORM}')
        os.system('cdk deploy --require-approval never')

# this function will log the state of the task at each iteration
# to help debug
def wait_ecs_tasks(ecs_cluster_name, task_arn):
    running = True
    attempts = 0
    failures = 0
    print(f'Waiting on task_arn={task_arn}', flush=True)
    client = boto3.client('ecs')

    while running:
        if attempts > 0:
            __sleep(WAITER_SLEEP, "Waiting to poll for task status, taskarn={}".format(task_arn))
        attempts += 1
        response = client.describe_tasks(
                cluster=ecs_cluster_name,
                tasks=[
                    task_arn,
                ]
            )
        print(f'describe_task_wait_on={response}', flush=True)
        if len(response['failures']) > 0:
            # above we print the full actual reponse for debugging
            print('decribe_task failure', flush=True)
            failures += 1
            if failures >= MAX_WAITER_DESCRIBE_FAILURES:
                break
            continue
        status = response['tasks'][0]['lastStatus']
        print(f'task {task_arn} is {status}', flush=True)
        # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-lifecycle.html
        if status == 'STOPPED' or status == 'DELETED':
            running = False
        if attempts >= MAX_WAITER_ATTEMPTS:
            print(f'stopped tasks waiter failed after {MAX_WAITER_ATTEMPTS}', flush=True)
            running = False


# For tests on ECS, we need to:
#  1. generate and register task definitions based on templates at /load_tests/task_definitons
#  2. run tasks with different throughput levels for 10 mins
#  3. wait until tasks completed, set buffer for logs sent to corresponding destinations
#  4. validate logs and print the result
def run_ecs_tests():
    ecs_cluster_name = os.environ['ECS_CLUSTER_NAME']
    names = {}

    # Run ecs tests once per input logger type
    test_results = []
    for input_logger in INPUT_LOGGERS:
        client = boto3.client('ecs')

        # S3 Fluent Bit extra config data
        s3_fluent_config_arn = publish_fluent_config_s3(input_logger)

        # Run ecs tasks and store task arns
        for throughput in THROUGHPUT_LIST:
            os.environ['THROUGHPUT'] = throughput
            generate_task_definition(throughput, input_logger, s3_fluent_config_arn)
            response = client.run_task(
                    cluster=ecs_cluster_name,
                    launchType='EC2',
                    taskDefinition=f'{PREFIX}{OUTPUT_PLUGIN}-{throughput}-{input_logger["name"]}'
            )
            print(f'run_task_response={response}', flush=True)
            names[f'{OUTPUT_PLUGIN}_{input_logger["name"]}_{throughput}_task_arn'] = response['tasks'][0]['taskArn']

        # Validation input type banner
        print(f'\nTest {input_logger["name"]} to {OUTPUT_PLUGIN} in progress...', flush=True)

    # Tasks need time to run
    __sleep(LOGGER_RUN_TIME_IN_SECOND, "Waiting for tasks to have time to run")

    # wait for tasks and validate
    for input_logger in INPUT_LOGGERS:
        # Wait until task stops and start validation
        processes = []

        for throughput in THROUGHPUT_LIST:
            client = boto3.client('ecs')
            task_arn = names[f'{OUTPUT_PLUGIN}_{input_logger["name"]}_{throughput}_task_arn']
            wait_ecs_tasks(ecs_cluster_name, task_arn)
            response = client.describe_tasks(
                cluster=ecs_cluster_name,
                tasks=[
                    task_arn,
                ]
            )
            print(f'task_arn={task_arn}', flush=True)
            print(f'describe_tasks_response={response}', flush=True)
            input_record = calculate_total_input_number(throughput)
            if len(response['failures']) == 0:
                check_app_exit_code(response)
                start_time = response['tasks'][0]['startedAt']
                stop_time = response['tasks'][0]['stoppedAt']
                log_delay = get_log_delay(parse_time(stop_time)-parse_time(start_time)-LOGGER_RUN_TIME_IN_SECOND)
                set_buffer(parse_time(stop_time))
            else:
                # missing tasks might mean the task stopped some time ago
                # and ECS already reaped/deleted it
                # try skipping straight to validation
                log_delay = 'unavailable' # we don't actually use this right now in results

            # Validate logs
            os.environ['LOG_SOURCE_NAME'] = input_logger["name"]
            os.environ['LOG_SOURCE_IMAGE'] = input_logger["logger_image"]
            validated_input_prefix = get_validated_input_prefix(input_logger)
            input_configuration = resource_resolver.get_input_configuration(PLATFORM, validated_input_prefix, throughput)
            test_configuration = {
                "input_configuration": input_configuration,
            }

            if OUTPUT_PLUGIN == 'cloudwatch':
                log_prefix = resource_resolver.get_destination_cloudwatch_prefix(test_configuration["input_configuration"])
            else:
                log_prefix = resource_resolver.get_destination_s3_prefix(test_configuration["input_configuration"], OUTPUT_PLUGIN)

            exec_args = ['go', 'run', './load_tests/validation/validate.go',
                '-input-record', input_record,
                '-log-delay', log_delay,
                '-region', AWS_REGION,
                '-bucket', S3_BUCKET_NAME,
                '-log-group', LOG_GROUP_NAME,
                '-prefix', log_prefix,
                '-destination', OUTPUT_PLUGIN,
            ]
            print("Running validator process. cmd=[{}]".format(' '.join(exec_args)), flush=True)
            processes.append({
                "input_logger": input_logger,
                "test_configuration": test_configuration,
                "process": subprocess.Popen(exec_args, stdout=subprocess.PIPE)
            })

        # Wait until all subprocesses for validation completed
        for p in processes:
            print("Waiting for validator process to complete cmd=[{}]".format(' '.join(p["process"].args)), flush=True)
            p["process"].wait()
            stdout, stderr = p["process"].communicate()
            return_code = p["process"].returncode
            print(f'{input_logger["name"]} to {OUTPUT_PLUGIN} raw validator stdout: {stdout}', flush=True)
            print(f'{input_logger["name"]} to {OUTPUT_PLUGIN} raw validator stderr: {stderr}', flush=True)
            print(f'{input_logger["name"]} to {OUTPUT_PLUGIN} raw validator return code: {return_code}', flush=True)
            p["result"] = stdout
        print(f'Test {input_logger["name"]} to {OUTPUT_PLUGIN} complete.', flush=True)

        parsedValidationOutputs = list(map(lambda p: {
            **p,
            "parsed_validation_output": parse_validation_output(p["result"])
        }, processes))

        test_results.extend(parsedValidationOutputs)

    # Print output
    print("\n\nValidation results:\n", flush=True)
    print(format_test_results_to_markdown(test_results), flush=True)

    # Bar check
    if not validation_bar.bar_raiser(test_results):
        print("Failed validation bar.", flush=True)
        sys.exit("Failed to pass the test_results validation bar")
    else:
        print("Passed validation bar.", flush=True)

def parse_validation_output(validationResultString):
    return { x[0]: x[1] for x in list(
        filter(lambda f: len(f) == 2,
            map(lambda x: x.split(",  "), validationResultString.decode("utf-8").split("\n"))
        ))}

def get_validation_output(logger_name, throughput, test_results):
    return list(filter(lambda r: r["input_logger"]["name"] == logger_name and
            int(r["test_configuration"]["input_configuration"]["throughput"].replace("m", "")) == throughput, test_results))[0]["parsed_validation_output"]

def format_test_results_to_markdown(test_results):
    # Configurable success character
    no_problem_cell_character = u"\U00002705" # This is a green check mark

    # Get table dimensions
    logger_names = list(set(map(lambda p: p["input_logger"]["name"], test_results)))
    logger_names.sort()
    plugin_name = PLUGIN_NAME_MAPS[OUTPUT_PLUGIN]
    throughputs = list(set(map(lambda p: int(p["test_configuration"]["input_configuration"]["throughput"].replace("m", "")), test_results)))
    throughputs.sort()

    # | plugin                   | source               |                            | 10 MB/s       | 20 MB/s       | 30 MB/s       |\n"
    # |--------------------------|----------------------|----------------------------|---------------|---------------|---------------|\n"
    col1_len = len(" plugin                   ")
    col2_len = len(" source               ")
    col3_len = len("                            ")
    colX_len = len(" 10 MB/s       ")

    output  = f'|{" plugin".ljust(col1_len)}|{" source".ljust(col2_len)}|{"".ljust(col3_len)}|'
    for throughput in throughputs:
        output += (" " + str(throughput) + " MB/s").ljust(colX_len) + "|"
    output += f"\n|{'-'*col1_len}|{'-'*col2_len}|{'-'*col3_len}|"
    for throughput in throughputs:
        output += f"{'-'*colX_len}|"
    output += "\n"

    # | kinesis_firehose          |  stdout             | Log Loss                   |               |               |               |\n"
    for logger_name in logger_names:
        output += "|"
        output += (" " + plugin_name).ljust(col1_len) + "|"
        output += (" " + logger_name).ljust(col2_len) + "|"
        output += (" Log Loss").ljust(col3_len) + "|"

        for throughput in throughputs:
            validation_output = get_validation_output(logger_name, throughput, test_results)

            if (int(validation_output["missing"]) != 0):
                output += (str(validation_output["percent_loss"]) + "%(" + str(validation_output["missing"]) + ")").ljust(colX_len)
            else:
                output += (" " + no_problem_cell_character).ljust(colX_len)

            output += "|"
        output += "\n"

        output += "|"
        output += (" ").ljust(col1_len) + "|"
        output += (" ").ljust(col2_len) + "|"
        output += (" Log Duplication").ljust(col3_len) + "|"

        for throughput in throughputs:
            validation_output = get_validation_output(logger_name, throughput, test_results)

            duplication_percent = (0 if int(validation_output["duplicate"]) == 0
                else math.floor(int(validation_output["duplicate"]) / int(validation_output["total_destination"]) * 100))

            if (int(validation_output["duplicate"]) != 0):
                output += (str(duplication_percent) + "%(" + str(validation_output["duplicate"]) + ")").ljust(colX_len)
            else:
                output += (" " + no_problem_cell_character).ljust(colX_len)

            output += "|"
        output += "\n"
    return output

def parse_json_template(template, dict):
    data = template
    for key in dict:
            if(key[0] == '$'):
                data = data.replace(key, dict[key])
            else:
                for sub_key in dict[key]:
                    data = data.replace(sub_key, dict[key][sub_key])
    return data

# Returns s3 arn
def publish_fluent_config_s3(input_logger):
    s3 = boto3.client('s3')
    s3.upload_file(
        input_logger["fluent_config_file_path"],
        S3_BUCKET_NAME,
        f'{OUTPUT_PLUGIN}-test/{PLATFORM}/fluent-{input_logger["name"]}.conf',
    )
    return f'arn:aws:s3:::{S3_BUCKET_NAME}/{OUTPUT_PLUGIN}-test/{PLATFORM}/fluent-{input_logger["name"]}.conf'

# The following method is used to clear data after all tests run.
# We set retention/expiration policies so that tests do not interfere with each other, and so that
# we can debug and run validation manually if necessary.
def delete_testing_data(session):
    print("Setting auto-delete policies for CW log groups, deleting S3 bucket")
    retention_days = 5

    logs_client = session.client('logs')
    try:
        # Set the retention policy for the log group
        response = logs_client.put_retention_policy(
            logGroupName=LOG_GROUP_NAME,
            retentionInDays=retention_days
        )
        print(f"Retention policy set successfully for log group. logGroupName={LOG_GROUP_NAME} retentionDays={retention_days}")
    except Exception as e:
        print(f"Error setting retention policy: {e}")

    # Empty s3 bucket
    # lifecycle config cannot currently be used because the bucket name
    # is reused between tests, so it must be completely deleted after each test.
    s3 = session.resource('s3')
    bucket = s3.Bucket(S3_BUCKET_NAME)
    print(f"Deleting all objects in s3 bucket: {S3_BUCKET_NAME}")
    bucket.objects.all().delete()

def generate_daemonset_config(throughput):
    daemonset_config_dict = {
        '$THROUGHPUT': throughput,
        '$FLUENT_BIT_IMAGE': os.environ['FLUENT_BIT_IMAGE'],
        '$APP_IMAGE': os.environ['EKS_APP_IMAGE'],
        '$TIME': str(LOGGER_RUN_TIME_IN_SECOND),
        '$CW_LOG_GROUP_NAME': LOG_GROUP_NAME,
    }
    fin = open(f'./load_tests/daemonset/{OUTPUT_PLUGIN}.yaml', 'r')
    data = fin.read()
    for key in daemonset_config_dict:
        data = data.replace(key, daemonset_config_dict[key])
    fout = open(f'./load_tests/daemonset/{OUTPUT_PLUGIN}_{throughput}.yaml', 'w')
    fout.write(data)
    fout.close()
    fin.close()

def run_eks_tests():
    client = boto3.client('logs')
    processes = set()

    for throughput in THROUGHPUT_LIST:
        generate_daemonset_config(throughput)
        os.system(f'kubectl apply -f ./load_tests/daemonset/{OUTPUT_PLUGIN}_{throughput}.yaml')
    # wait (10 mins run + buffer for setup/log delivery)
    __sleep(1000, "Waiting 10 minutes+buffer to setup and log delivery")
    for throughput in THROUGHPUT_LIST:
        input_record = calculate_total_input_number(throughput)
        response = client.describe_log_streams(
            logGroupName=LOG_GROUP_NAME,
            logStreamNamePrefix=f'{PREFIX}kube.var.log.containers.ds-cloudwatch-{throughput}',
            orderBy='LogStreamName'
        )
        for log_stream in response['logStreams']:
            if 'app-' not in log_stream['logStreamName']:
                continue
            expect_time = log_stream['lastEventTimestamp']
            actual_time = log_stream['lastIngestionTime']
            log_delay = get_log_delay(actual_time/1000-expect_time/1000)
            log_prefix = resource_resolver.get_destination_cloudwatch_prefix(test_configuration["input_configuration"])
            exec_args = ['go', 'run', './load_tests/validation/validate.go',
                         '-input-record', input_record,
                         '-log-delay', log_delay,
                         '-region', AWS_REGION,
                         '-bucket', S3_BUCKET_NAME,
                         '-log-group', LOG_GROUP_NAME,
                         '-prefix', log_prefix,
                         '-destination', OUTPUT_PLUGIN]
            processes.add(subprocess.Popen(exec_args))

    # Wait until all subprocesses for validation completed
    for p in processes:
        p.wait()

def delete_testing_resources():
    print("Deleting test resources")
    # Create sts session
    session = get_sts_boto_session()

    # delete all logs uploaded by Fluent Bit
    # delete all S3 config files
    delete_testing_data(session)

    print(f"Deleting cloudformation stack. stackName={TESTING_RESOURCES_STACK_NAME}", flush=True)
    # All related testing resources will be destroyed once the stack is deleted
    client = session.client('cloudformation')
    client.delete_stack(
        StackName=TESTING_RESOURCES_STACK_NAME
    )
    # scale down eks cluster
    if PLATFORM == 'eks':
        print("Scaling down EKS cluster", flush=True)
        os.system('kubectl delete namespace load-test-fluent-bit-eks-ns')
        os.system(f'eksctl scale nodegroup --cluster={EKS_CLUSTER_NAME} --nodes=0 ng')

def get_validated_input_prefix(input_logger):
    # Prefix used to form destination identifier
    # [log source] ----- (stdout) -> std-{{throughput}}/...
    #               \___ (tcp   ) -> {{throughput}}/...
    #
    # All inputs should have throughput as destination identifier
    # except stdstream
    if (input_logger['name'] == 'stdstream'):
        return resource_resolver.STD_INPUT_PREFIX
    return resource_resolver.CUSTOM_INPUT_PREFIX

def get_sts_boto_session():
    # STS credentials
    sts_client = boto3.client('sts')

    # Call the assume_role method of the STSConnection object and pass the role
    # ARN and a role session name.
    assumed_role_object = sts_client.assume_role(
        RoleArn=os.environ["LOAD_TEST_CFN_ROLE_ARN"],
        RoleSessionName="load-test-cfn",
        DurationSeconds=3600
    )

    # From the response that contains the assumed role, get the temporary
    # credentials that can be used to make subsequent API calls
    credentials=assumed_role_object['Credentials']

    # Create boto session
    return boto3.Session(
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )

if sys.argv[1] == 'create_testing_resources':
    create_testing_resources()
elif sys.argv[1] == 'ECS':
    run_ecs_tests()
elif sys.argv[1] == 'EKS':
    run_eks_tests()
elif sys.argv[1] == 'delete_testing_resources':
    # testing resources only need to be deleted once
    if OUTPUT_PLUGIN == 'cloudwatch':
        delete_testing_resources()
