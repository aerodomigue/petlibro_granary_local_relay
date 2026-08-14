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
	readHeaderTimeout    = 5 * time.Second
)

func main() {
	listenAddress := os.Getenv("CAMERA_BRIDGE_LISTEN_ADDR")
	if listenAddress == "" {
		listenAddress = defaultListenAddress
	}

	server := &http.Server{
		Addr:              listenAddress,
		Handler:           bridge.NewHandler(bridge.NewRegistryWithBroadcastFallback(broadcastFallback())),
		ReadHeaderTimeout: readHeaderTimeout,
	}
	log.Printf("camera bridge listening on %s", listenAddress)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
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
