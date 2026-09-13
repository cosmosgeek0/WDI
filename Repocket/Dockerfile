ARG TOOLBOX_IMAGE=busybox@sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23
ARG REPOCKET_IMAGE=repocket/repocket@sha256:cb390c16d8989fdd91dde6ec8c74a8ebac771b5656acb06766dba938adf8d9ea
FROM ${TOOLBOX_IMAGE} AS toolbox
FROM ${REPOCKET_IMAGE}
COPY --from=toolbox /bin/ /bin/
COPY start.sh /usr/local/bin/repocket-start
ENTRYPOINT ["/bin/busybox", "sh", "/usr/local/bin/repocket-start"]
