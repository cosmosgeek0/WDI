ARG GRASS_BASE=wdi-grass-base:7.6.1-v3
ARG REPOCKET_BASE=163092837436.dkr.ecr.ap-south-1.amazonaws.com/grass-desktop@sha256:45cd81b05271de652259fdbed044505b41fe770755192c4b99eb370c30df0647
FROM ${REPOCKET_BASE} AS repocket
FROM scratch AS additions
COPY --from=repocket /usr/local/bin/repocket /usr/local/bin/repocket-start /usr/local/bin/
COPY --from=repocket /bin/busybox /usr/bin/busybox
COPY strace /usr/local/bin/strace
COPY traffic.py /opt/app-traffic/traffic.py
COPY LICENSE.strace /opt/app-traffic/LICENSE.strace
COPY supervisor.py session.py worker.pl worker.sha256 ip_policy.py node_auth.py /opt/grass-private/
FROM ${GRASS_BASE}
USER 0:0
COPY --from=additions / /
RUN useradd --uid 1001 --create-home --home-dir /repocket --shell /usr/sbin/nologin repocket \
    && install -d -o 1001 -g 1001 -m 700 /repocket/run /repocket/tmp \
    && install -d -m 755 /run/wdi \
    && chown -R 0:0 /opt/grass-private /opt/app-traffic \
    && chmod 755 /opt/grass-private /opt/app-traffic \
    && chmod 700 /config /repocket \
    && python3 /opt/grass-private/node_auth.py
