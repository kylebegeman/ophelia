from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200 if self.path in {"/", "/health"} else 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"fixture-http-service")


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

