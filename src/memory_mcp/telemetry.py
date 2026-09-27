"""OpenTelemetry bootstrap — call once at app startup."""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

log = logging.getLogger(__name__)


def setup_telemetry(otlp_endpoint: str, service_name: str = "memory-mcp") -> None:
    """Configure OTel metrics + traces exporting via OTLP gRPC.

    Resource attributes are built with ``Resource.create()`` so the standard
    OTel env vars ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES`` are
    honored/merged. This lets multiple deployments of this same image be told
    apart downstream — e.g. the prod (namespace ``digital-twin``) and dev
    (``digital-twin-dev``) instances both export to the shared mcit-k8s
    otel-collector -> Application Insights, and would otherwise collide on the
    same ``cloud_RoleName`` (the azure_monitor exporter derives it from
    ``service.namespace``/``service.name``). Set ``OTEL_SERVICE_NAME`` and/or
    ``OTEL_RESOURCE_ATTRIBUTES=service.namespace=<ns>`` per deployment to
    distinguish them.

    Precedence: if ``OTEL_SERVICE_NAME`` is set it wins; otherwise the
    ``service_name`` arg (default ``memory-mcp``) is used — so existing
    deployments with neither env set are unchanged."""
    attrs = {}
    if "OTEL_SERVICE_NAME" not in os.environ:
        attrs[SERVICE_NAME] = service_name
    resource = Resource.create(attrs)

    # Metrics
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
        export_interval_millis=30_000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # Traces
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)))
    trace.set_tracer_provider(tp)

    log.info("OTel telemetry configured → %s", otlp_endpoint)
