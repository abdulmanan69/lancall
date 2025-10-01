#!/usr/bin/env python3
"""
HTTPS server for the LAN voice call application
"""

import http.server
import ssl
import socket
import os

def get_local_ip():
    """Get the local IP address"""
    try:
        # Connect to a remote address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def run_https_server():
    """Run the HTTPS server"""
    # Check if certificate files exist
    if not os.path.exists("server.crt") or not os.path.exists("server.key"):
        print("Certificate files not found!")
        print("Please run 'python generate_cert.py' first to generate them.")
        return
    
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Change to the script directory to serve files from there
    os.chdir(script_dir)
    
    # Set up the HTTP server
    server_address = ('0.0.0.0', 8443)
    
    # Create HTTP server
    httpd = http.server.HTTPServer(server_address, http.server.SimpleHTTPRequestHandler)
    
    # Create SSL context (modern approach)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('server.crt', 'server.key')
    
    # Wrap the HTTP server with SSL
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    
    local_ip = get_local_ip()
    
    print("=" * 50)
    print("HTTPS Server for LAN Voice Call")
    print("=" * 50)
    print(f"Server running at https://{local_ip}:8443")
    print(f"Local access: https://localhost:8443")
    print(f"Local access: https://127.0.0.1:8443")
    print("")
    print("IMPORTANT:")
    print("- Your browser will show a security warning because this is a self-signed certificate")
    print("- This is normal for development. Click 'Advanced' and 'Proceed to ...' to continue")
    print("- All devices on the same network can access this URL")
    print("- Media device access (microphone) will work properly with HTTPS")
    print("")
    print("Press Ctrl+C to stop the server")
    print("=" * 50)
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")

if __name__ == "__main__":
    run_https_server()