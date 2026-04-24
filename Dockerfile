FROM alpine:latest
RUN apk add --no-cache python3 iproute2
RUN mkdir -p /app
WORKDIR /app
COPY router.py /app/router.py
CMD ["python3", "-u", "router.py"]
