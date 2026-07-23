ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS runtime

FROM scratch
COPY --from=runtime / /

COPY --chown=65532:65532 rootfs/ /

USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["/opt/unified-ext-lab/guest.py"]
CMD ["idle"]
