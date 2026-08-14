FROM golang:1.24-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /camera-bridge ./cmd/petlibro-camera-bridge

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /camera-bridge /camera-bridge
EXPOSE 8081
USER nonroot:nonroot
ENTRYPOINT ["/camera-bridge"]
