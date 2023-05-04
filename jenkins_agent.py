#!/usr/bin/env python3

import argparse
import json
import logging
import os
import os.path
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GCE_METADATA_ENDPOINT = 'http://metadata.google.internal/computeMetadata/v1/instance'
JENKINS_AGENT_JAR_PATH = 'jnlpJars/agent.jar'
JENKINS_AGENT_JAR_LOCAL_PATH = '/tmp/agent.jar'
JENKINS_AGENT_LOCAL_HOME_PATH = '/home/ubuntu'


def get_credentialed_session(credentials):
    session = requests.Session()
    username, password = credentials.split(':')
    session.auth = (username, password)
    return session


def load_instance_metadata_item(path):
    headers = {
        'Metadata-Flavor': 'Google',
    }
    url = f'{GCE_METADATA_ENDPOINT}/{path}'
    response = requests.get(url, headers=headers)
    if response.status_code == requests.codes.ok:
        return response.content.decode('utf-8')
    response.raise_for_status()


def load_instance_metadata():
    logger.info('Retrieving GCE internal computeMetadata')
    node_name = load_instance_metadata_item('name')
    jenkins_url = load_instance_metadata_item('attributes/JENKINS_URL')
    jnlp_credentials = load_instance_metadata_item('attributes/JNLP_CREDENTIALS')
    jenkins_label = load_instance_metadata_item('attributes/JENKINS_AGENT_LABEL')

    instance_data = {
        'jenkins_url': jenkins_url.strip('/'),
        'name': node_name,
        'jnlp_credentials': jnlp_credentials,
        'jenkins_label': jenkins_label,
    }
    return instance_data


def _deregister_with_jenkins_master(jenkins_url, name, credentials):
    logger.info(f'De-registering worker {name} from Jenkins {jenkins_url}')
    delete_url = f'{jenkins_url}/computer/{name}/doDelete'

    session = get_credentialed_session(credentials)

    # Reverse-engineered from the manual delete page form using http://requestb.in/
    data = {
        'json': '{"Submit": "Yes"}',
        'Submit': 'Yes',
    }
    delete_r = session.post(delete_url, data, allow_redirects=False)
    # Jenkins provides a 302 response for a successful deletion request
    if delete_r.status_code == requests.codes.found:
        logger.info(f'Worker {name} successfully de-registered from Jenkins {jenkins_url}')
        return

    if not _is_already_registered(jenkins_url, name, session):
        # This might be a retry of the shutdown process, which means a
        # prior de-registration succeeded.
        logger.info(f'Worker {name} already de-registered')
        return

    logger.critical(f'Error de-registering jenkins worker at URL: {delete_url}')
    delete_r.raise_for_status()


def _is_already_registered(jenkins_url, name, session):
    logger.info(f'Checking if worker {name} is already registered')
    node_list_url = f'{jenkins_url}/computer/api/json?tree=computer[displayName]'

    response = session.get(node_list_url)
    if response.status_code != requests.codes.ok:
        logger.critical(f'Error checking for existing workers at URL: {node_list_url}')
        response.raise_for_status()

    node_list = response.json()
    computers = node_list.get('computer', [])
    for computer in computers:
        if computer.get('displayName') == name:
            logger.info(f'Worker {name} already registered')
            return True

    return False


def _do_registration(jenkins_url, name, session, jenkins_label):
    remote_fs = JENKINS_AGENT_LOCAL_HOME_PATH

    logger.info(f'Registering worker {name} with Jenkins {jenkins_url}')
    params = {
        'name': name,
        'type': 'hudson.slaves.DumbSlave$DescriptorImpl',
        'json': json.dumps({
            'name': name,
            'nodeDescription': None,
            'numExecutors': 1,
            'remoteFS': remote_fs,
            'labelString': jenkins_label,
            'mode': 'NORMAL',
            'type': 'hudson.slaves.DumbSlave$DescriptorImpl',
            'retentionStrategy': {'stapler-class': 'hudson.slaves.RetentionStrategy$Always'},  # noqa
            'nodeProperties': {'stapler-class-bag': 'true'},
            'launcher': {'stapler-class': 'hudson.slaves.JNLPLauncher'}
        })
    }
    params_encoded = urlencode(params)
    create_url = f'{jenkins_url}/computer/doCreateItem?{params_encoded}'

    r = session.post(create_url, allow_redirects=False)
    if r.status_code != requests.codes.found:
        logger.critical(f'Error registering jenkins worker at URL: {create_url}')
        r.raise_for_status()

    jenkins_env = {
        'HOME': remote_fs,
    }

    # jenkins worker API can be viewed at
    # http://<domain>/computer/<worker-name>/api/

    # The config.xml can be viewed at
    # http://<domain>/computer/<worker-name>/config.xml

    # If the XML below stops working, manually create a worker
    # and then access the config.xml URL to generate it

    jenkins_env_string = ''.join(
        f'<string>{key}</string><string>{value}</string>'
        for key, value in jenkins_env.items()
    )
    config_xml = f'''
    <slave>
        <name>{name}</name>
        <description/>
        <remoteFS>{remote_fs}</remoteFS>
        <numExecutors>1</numExecutors>
        <mode>EXCLUSIVE</mode>
        <retentionStrategy class="hudson.slaves.RetentionStrategy$Always"/>
        <launcher class="hudson.slaves.JNLPLauncher">
            <webSocket>true</webSocket>
        </launcher>
        <label>{jenkins_label}</label>
        <nodeProperties>
            <hudson.slaves.EnvironmentVariablesNodeProperty>
                <envVars serialization="custom">
                    <unserializable-parents/>
                    <tree-map>
                        <default>
                            <comparator class="hudson.util.CaseInsensitiveComparator"/>
                        </default>
                        <int>{len(jenkins_env)}</int>
                        {jenkins_env_string}
                    </tree-map>
                </envVars>
            </hudson.slaves.EnvironmentVariablesNodeProperty>
        </nodeProperties>
    </slave>
    '''
    update_url = f'{jenkins_url}/computer/{name}/config.xml'

    logger.info(f'Updating {update_url}')
    response = session.post(update_url, data=config_xml, allow_redirects=False)
    if response.status_code != requests.codes.ok:
        response.raise_for_status()


def handle_shutdown():
    # Deregister this Jenkins worker so that Jenkins doesn't try to check it
    # for availability and delete the cloud VM so that e.g. GCE doesn't keep it
    # around in the "Stopped" state.
    data = load_instance_metadata()
    jenkins_url = data['jenkins_url']
    name = data['name']
    credentials = data['jnlp_credentials']

    _deregister_with_jenkins_master(jenkins_url, name, credentials)


def _get_jnlp_secret(jenkins_url, name, session):
    logger.info(f'Retrieving jnlp secret for worker {name}')

    url = f'{jenkins_url}/computer/{name}/jenkins-agent.jnlp'
    response = session.get(url)
    if response.status_code != requests.codes.ok:
        logger.critical(f'Error fetching {url}')
        response.raise_for_status()

    xml_content = response.content.decode('utf-8')
    root = ET.fromstring(xml_content)
    jnlp_secret = root.find('application-desc').find('argument').text
    return jnlp_secret


def handle_start():
    data = load_instance_metadata()

    name = data['name']
    jenkins_url = data['jenkins_url']
    jnlp_credentials = data['jnlp_credentials']
    jenkins_label = data['jenkins_label']

    if not os.path.exists(JENKINS_AGENT_JAR_LOCAL_PATH):
        agent_jar_url = f'{jenkins_url}/{JENKINS_AGENT_JAR_PATH}'
        logger.info(f'Fetching {agent_jar_url}')
        wget_command = f'wget {agent_jar_url} -O {JENKINS_AGENT_JAR_LOCAL_PATH}'
        os.system(wget_command)

    session = get_credentialed_session(jnlp_credentials)
    if not _is_already_registered(jenkins_url, name, session):
        _do_registration(jenkins_url, name, session, jenkins_label)

    jnlp_secret = _get_jnlp_secret(jenkins_url, name, session)

    # -noReconnect means that we're relying on upstart to respawn the process
    # if we temporarily lose connection.
    java_jar_agent_command = (
        f'java -jar {JENKINS_AGENT_JAR_LOCAL_PATH}'
        ' -noReconnect'
        f' -jnlpUrl {jenkins_url}/computer/{name}/jenkins-agent.jnlp'
        f' -secret {jnlp_secret}'
    )

    logger.info('Starting agent and connecting over jnlp')
    os.system(java_jar_agent_command)


def main():
    ap = argparse.ArgumentParser()
    subparsers = ap.add_subparsers(title='subcommands', dest='command')
    subparsers.required = True

    start_parser = subparsers.add_parser(
        'start',
        help='Register with jenkins and start the jenkins agent',
    )
    start_parser.set_defaults(func=handle_start)

    shutdown_parser = subparsers.add_parser(
        'shutdown',
        help='Deregister with jenkins',
    )
    shutdown_parser.set_defaults(func=handle_shutdown)

    args = ap.parse_args()
    args.func()


if __name__ == '__main__':
    main()
