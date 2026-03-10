FROM golang:1.25-bookworm AS build

WORKDIR /src
COPY go.mod ./
RUN go mod download

COPY . .
ARG APP_BIN=api
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "/out/holodub" "./cmd/${APP_BIN}" \
    && test -s "/out/holodub"

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /out/holodub /usr/local/bin/holodub

ENV DATA_ROOT=/data
CMD ["/usr/local/bin/holodub"]
