package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
)

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		json.NewEncoder(w).Encode(map[string]string{"app": "{{app_name}}", "message": "سلام از لیارا! این برنامه با قالب Go نگهبان ساخته شد."})
	})
	mux.HandleFunc("{{health_path}}", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	port := os.Getenv("PORT")
	if port == "" {
		port = "{{port}}"
	}
	log.Printf("listening on :%s", port)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}
