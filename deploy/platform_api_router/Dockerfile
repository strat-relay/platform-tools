FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

COPY deploy/platform_api_router/nginx.conf /etc/nginx/nginx.conf

EXPOSE 22351

USER 101:101
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
