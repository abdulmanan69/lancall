#!/usr/bin/env python3
"""
Generate a self-signed certificate for HTTPS development
"""

import os
import socket
from OpenSSL import crypto

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

def generate_self_signed_cert():
    """Generate a self-signed certificate"""
    # Create a key pair
    key = crypto.PKey()
    key.generate_key(crypto.TYPE_RSA, 2048)

    # Create a self-signed cert
    cert = crypto.X509()
    cert.get_subject().C = "US"
    cert.get_subject().ST = "Development"
    cert.get_subject().L = "Local"
    cert.get_subject().O = "LAN Voice Call"
    cert.get_subject().OU = "Development"
    cert.get_subject().CN = get_local_ip()
    cert.set_serial_number(1000)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365*24*60*60)  # 1 year
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(key)
    cert.sign(key, 'sha256')

    # Write the private key and certificate to files
    with open("server.key", "wb") as key_file:
        key_file.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))
    
    with open("server.crt", "wb") as cert_file:
        cert_file.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    
    print("Self-signed certificate generated successfully!")
    print(f"Certificate: server.crt")
    print(f"Private Key: server.key")
    print(f"Use these files to run the HTTPS server")

if __name__ == "__main__":
    generate_self_signed_cert()