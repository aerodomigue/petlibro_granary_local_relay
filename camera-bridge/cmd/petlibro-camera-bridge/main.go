// Command petlibro-camera-bridge owns the narrow local camera registration API.
package main

import (
	"log"
	"net/http"
	"os"
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
		Handler:           bridge.NewHandler(bridge.NewRegistry()),
		ReadHeaderTimeout: readHeaderTimeout,
	}
	log.Printf("camera bridge listening on %s", listenAddress)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
