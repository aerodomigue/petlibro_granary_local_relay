// Command petlibro-camera-bridge owns the narrow local camera registration API.
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/aerodomigue/petlibro-camera-bridge/internal/bridge"
)

const (
	defaultListenAddress = ":8081"
	defaultMediaAddress  = ":8554"
	readHeaderTimeout    = 5 * time.Second
)

func main() {
	listenAddress := os.Getenv("CAMERA_BRIDGE_LISTEN_ADDR")
	if listenAddress == "" {
		listenAddress = defaultListenAddress
	}

	registry := bridge.NewRegistryWithBroadcastFallback(broadcastFallback())
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
