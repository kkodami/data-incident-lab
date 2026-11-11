FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
ENTRYPOINT ["incident-lab"]
CMD ["demo", "--dir", "/data/demo"]
