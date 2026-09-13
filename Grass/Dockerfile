ARG GRASS_BASE=wdi-grass-base:7.6.1-v3
ARG REPOCKET_BASE=163092837436.dkr.ecr.ap-south-1.amazonaws.com/grass-desktop@sha256:45cd81b05271de652259fdbed044505b41fe770755192c4b99eb370c30df0647
FROM ${REPOCKET_BASE} AS repocket
FROM scratch AS additions
COPY --from=repocket /usr/local/bin/repocket /usr/local/bin/repocket-start /usr/local/bin/
COPY --from=repocket /bin/busybox /usr/bin/busybox
COPY strace /usr/local/bin/strace
COPY traffic.py /opt/app-traffic/traffic.py
COPY LICENSE.strace /opt/app-traffic/LICENSE.strace
COPY --chown=1000:1000 supervisor.py session.py worker.pl worker.sha256 ip_policy.py /opt/grass-private/
FROM ${GRASS_BASE}
COPY --from=additions / /
USER 1000:1000
