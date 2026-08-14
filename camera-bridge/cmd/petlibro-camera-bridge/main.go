// Command petlibro-camera-bridge owns the narrow local camera registration API.
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/aerodomigue/petlibro-camera-bridge/internal/bridge"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

const (
	defaultListenAddress = ":8081"
	defaultMediaAddress  = ":8554"
	defaultIdleTimeout   = 10 * time.Second
	readHeaderTimeout    = 5 * time.Second
)

func main() {
	listenAddress := os.Getenv("CAMERA_BRIDGE_LISTEN_ADDR")
	if listenAddress == "" {
		listenAddress = defaultListenAddress
	}

	registry := bridge.NewRegistryWithConnectorAndIdleTimeout(
		connectorWithBroadcastFallback(broadcastFallback()),
		idleTimeout(),
	)
	mediaServer, err := bridge.StartMediaServer(mediaListenAddress(), registry)
	if err != nil {
		log.Fatal(err)
	}
	defer func() { _ = mediaServer.Close() }()
	server := &http.Server{
		Addr:              listenAddress,
		Handler:           bridge.NewHandler(registry),
		ReadHeaderTimeout: readHeaderTimeout,
	}
	log.Printf("camera bridge listening on %s", listenAddress)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func connectorWithBroadcastFallback(enabled bool) *plaf203.DirectConnector {
	connector := plaf203.NewDirectConnector()
	connector.BroadcastFallback = enabled
	return connector
}

func mediaListenAddress() string {
	listenAddress := os.Getenv("PETLIBRO_CAMERA_MEDIA_RTSP_LISTEN_ADDR")
	if listenAddress == "" {
		return defaultMediaAddress
	}
	return listenAddress
}

func broadcastFallback() bool {
	value := os.Getenv("PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK")
	if value == "" {
		return true
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		log.Printf("invalid PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK=%q; using true", value)
		return true
	}
	return parsed
}

func idleTimeout() time.Duration {
	value := os.Getenv("PETLIBRO_CAMERA_IDLE_TIMEOUT_SECONDS")
	if value == "" {
		return defaultIdleTimeout
	}
	seconds, err := strconv.ParseFloat(value, 64)
	if err != nil || seconds < 1 {
		log.Printf("invalid PETLIBRO_CAMERA_IDLE_TIMEOUT_SECONDS=%q; using %s", value, defaultIdleTimeout)
		return defaultIdleTimeout
	}
	return time.Duration(seconds * float64(time.Second))
}
