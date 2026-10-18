# Published official runtime, never a locally customized Hermes checkout.
ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:648e4daf033bf2de209435fd9dd61ee3e11103c1f7e1ab469bffbfb99acc263f
FROM ${HERMES_IMAGE}
USER root
# Test tooling has its own environment; the official sealed runtime is untouched.
RUN /usr/bin/python3 -m venv /opt/lab-tools && \
    /opt/lab-tools/bin/python -m pip install --no-cache-dir \
    pytest==9.1.1 ruff==0.16.10 mypy==2.4.0 PyYAML==6.0.3 \
    build==1.6.1 setuptools==84.0.0 hermes-agent==0.19.0
COPY native_probe.py /opt/lab/native_probe.py
USER 10000:10000
ENTRYPOINT ["/opt/hermes/.venv/bin/python"]
CMD ["/opt/lab/native_probe.py"]
