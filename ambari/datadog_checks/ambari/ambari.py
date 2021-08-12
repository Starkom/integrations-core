# (C) Datadog, Inc. 2019-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from requests.exceptions import ConnectionError, HTTPError, Timeout
from six import iteritems

from datadog_checks.base import AgentCheck
from datadog_checks.base.errors import CheckException

from datadog_checks.base.constants import ServiceCheck

from . import common

# Tag templates
CLUSTER_TAG_TEMPLATE = "ambari_cluster:{}"
SERVICE_TAG = "ambari_service:"
COMPONENT_TAG = "ambari_component:"

# URL queries
COMPONENT_METRICS_QUERY = "/components?fields=metrics"
SERVICE_INFO_QUERY = "?fields=ServiceInfo"

# Response fields
METRICS_FIELD = "metrics"


class AmbariCheck(AgentCheck):
    def __init__(self, name, init_config, instances):
        super(AmbariCheck, self).__init__(name, init_config, instances)

        self.base_url = self.instance.get("url", "")
        self.base_tags = self.instance.get("tags", [])
        self.included_services = self.instance.get("services", [])

    def check(self, _):
        clusters = self.get_clusters()
        self.get_host_metrics(clusters)

        collect_service_metrics = self._should_collect_service_metrics()
        collect_service_status = self._should_collect_service_status()
        if collect_service_metrics or collect_service_status:
            self.get_service_status_and_metrics(clusters, collect_service_metrics, collect_service_status)

    def _should_collect_service_metrics(self):
        return self.init_config.get("collect_service_metrics", True)

    def _should_collect_service_status(self):
        return self.init_config.get("collect_service_status", False)

    def get_clusters(self):
        clusters_endpoint = common.CLUSTERS_URL.format(base_url=self.base_url)
        service_check_tags = self.base_tags + ["url:{}".format(self.base_url)]
        resp = self._make_request(clusters_endpoint)
        if resp is None:
            self._submit_service_checks("can_connect", self.CRITICAL, service_check_tags)
            raise CheckException(
                "Couldn't connect to URL: {}. Please verify the address is reachable".format(clusters_endpoint)
            )

        self._submit_service_checks("can_connect", self.OK, service_check_tags)
        return self._get_response_clusters(resp)

    def _get_response_clusters(self, resp):
        items = resp.get('items')
        if not items:
            self.log.warning('No clusters found')
            return []

        clusters = []
        for cluster in items:
            c = cluster.get('Clusters')
            if c:
                clusters.append(c.get('cluster_name'))

        return clusters

    def get_host_metrics(self, clusters):
        external_tags = []
        for cluster in clusters:
            cluster_tag = CLUSTER_TAG_TEMPLATE.format(cluster)
            hosts_list = self._get_hosts_info(cluster)

            for host in hosts_list:
                h = host.get('Hosts')
                if not h:
                    self.log.warning("Unexpected response format for host list")
                    continue
                hostname = h.get('host_name')
                if not hostname:
                    self.log.warning("Unexpected response format for host list")
                    continue

                external_tags.append((hostname, {'ambari': [cluster_tag]}))
                host_metrics = host.get(METRICS_FIELD)
                if host_metrics is None:
                    self.warning("No metrics received for host %s", hostname)
                    continue

                metrics = self.flatten_host_metrics(host_metrics)
                for metric_name, value in iteritems(metrics):
                    metric_tags = self.base_tags + [cluster_tag]
                    if isinstance(value, float):
                        self._submit_gauge(metric_name, value, metric_tags, hostname)
                    else:
                        self.warning("Expected a float for %s, received %s", metric_name, value)
        self.set_external_tags(external_tags)

    def get_service_status_and_metrics(self, clusters, collect_service_metrics, collect_service_status):
        if not self.included_services:
            self.log.warning("Service metrics or status collection activated but no services have been included")
            return

        for cluster in clusters:
            tags = self.base_tags + [CLUSTER_TAG_TEMPLATE.format(cluster)]
            for service, components in iteritems(self.included_services):
                service_tags = tags + [SERVICE_TAG + service.lower()]

                if collect_service_metrics:
                    self.get_component_metrics(cluster, service, service_tags, components)
                if collect_service_status:
                    self.get_service_checks(cluster, service, service_tags)

    def get_service_checks(self, cluster, service, service_tags):
        service_check_endpoint = common.create_endpoint(self.base_url, cluster, service, SERVICE_INFO_QUERY)
        service_resp = self._make_request(service_check_endpoint)
        if service_resp is None:
            self._submit_service_checks("state", self.CRITICAL, service_tags)
            self.warning("No response received for service %s", service)
        else:
            state = service_resp['ServiceInfo']['state']
            service_check_state = common.status_to_service_check(state)

            message = state
            if service_check_state is ServiceCheck.OK:
                message = ''

            self._submit_service_checks(
                "state", service_check_state, service_tags + ['state:%s' % state], message=message
            )

    def get_component_metrics(self, cluster, service, base_tags, component_included):
        if not component_included:
            self.log.warning("Service metrics collection activated but no components have been included")
            return

        component_metrics_endpoint = common.create_endpoint(self.base_url, cluster, service, COMPONENT_METRICS_QUERY)
        components_response = self._make_request(component_metrics_endpoint)
        component_included = {k.upper(): v for k, v in iteritems(component_included)}

        if components_response is None or 'items' not in components_response:
            self.log.warning("No components found for service %s.", service)
            return

        for component in components_response['items']:
            component_name = component['ServiceComponentInfo']['component_name']

            if component_name not in component_included:
                self.log.debug('Component %s not included', component_name)
                continue
            component_metrics = component.get(METRICS_FIELD)
            if component_metrics is None:
                # Not all components provide metrics
                self.log.debug("No metrics found for component %s for service %s", component_name, service)
                continue

            for header in component_included[component_name]:
                if header not in component_metrics:
                    self.log.warning(
                        "No %s metrics found for component %s for service %s", header, component_name, service
                    )
                    continue

                metrics = self.flatten_service_metrics(component_metrics[header], header)
                component_tag = COMPONENT_TAG + component_name.lower()
                for metric_name, value in iteritems(metrics):
                    metric_tags = base_tags + [component_tag]
                    if isinstance(value, float):
                        self._submit_gauge(metric_name, value, metric_tags)
                    else:
                        self.warning("Expected a float for %s, received %s", metric_name, value)

    def _get_hosts_info(self, cluster):
        hosts_endpoint = common.HOST_METRICS_URL.format(base_url=self.base_url, cluster_name=cluster)
        resp = self._make_request(hosts_endpoint)

        return resp.get('items')

    def _make_request(self, url):
        try:
            resp = self.http.get(url)
            return resp.json()
        except (HTTPError, ConnectionError) as e:
            self.warning(
                "Couldn't connect to URL: %s with exception: %s. Please verify the address is reachable", url, e
            )
        except Timeout:
            self.warning("Connection timeout when connecting to %s", url)

    def _submit_gauge(self, name, value, tags, hostname=None):
        self.gauge('{}.{}'.format(common.METRIC_PREFIX, name), value, tags, hostname=hostname)

    def _submit_service_checks(self, name, value, tags, message=None):
        self.service_check('{}.{}'.format(common.METRIC_PREFIX, name), value, tags, message=message)

    @classmethod
    def flatten_service_metrics(cls, metric_dict, prefix):
        flat_metrics = {}
        for raw_metric_name, metric_value in iteritems(metric_dict):
            if isinstance(metric_value, dict):
                flat_metrics.update(cls.flatten_service_metrics(metric_value, prefix))
            else:
                metric_name = '{}.{}'.format(prefix, raw_metric_name) if prefix else raw_metric_name
                flat_metrics[metric_name] = metric_value
        return flat_metrics

    @classmethod
    def flatten_host_metrics(cls, metric_dict, prefix=""):
        flat_metrics = {}
        for raw_metric_name, metric_value in iteritems(metric_dict):
            metric_name = '{}.{}'.format(prefix, raw_metric_name) if prefix else raw_metric_name
            if raw_metric_name == "boottime":
                flat_metrics["boottime"] = metric_value
            elif isinstance(metric_value, dict):
                flat_metrics.update(cls.flatten_host_metrics(metric_value, metric_name))
            else:
                flat_metrics[metric_name] = metric_value
        return flat_metrics
