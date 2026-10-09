FROM public.ecr.aws/docker/library/python:3.12-alpine

WORKDIR /app

COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

COPY app.py /app/
COPY API.md /app/

ENTRYPOINT ["/app/entrypoint.sh"]
