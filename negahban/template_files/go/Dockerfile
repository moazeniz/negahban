FROM golang:1.22-alpine AS build
WORKDIR /src
ENV CGO_ENABLED=0
COPY go.* ./
RUN go mod download
COPY . .
RUN go build -trimpath -ldflags="-s -w" -o /out/app .

FROM alpine:3.20
RUN adduser -D -u 1000 app
COPY --from=build /out/app /usr/local/bin/app
USER app
ENV PORT={{port}}
EXPOSE {{port}}
CMD ["app"]
