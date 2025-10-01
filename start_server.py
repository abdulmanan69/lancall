#!/usr/bin/env python3
"""
Start script for the LAN Voice Call HTTPS server
This script will:
1. Check if required libraries are installed
2. Install missing libraries if needed
3. Generate SSL certificates if they don't exist
4. Start both the HTTPS server and signaling server
"""

import sys
import os
import subprocess
import importlib.util
import threading
import time
import signal

def check_and_install_packages():
    """Check if required packages are installed and install them if missing"""
    required_packages = [
        ("OpenSSL", "pyOpenSSL"),
        ("websockets", "websockets"),
        ("http.server", None),  # Built-in module
        ("ssl", None),  # Built-in module
        ("socket", None)  # Built-in module
    ]
    
    missing_packages = []
    
    print("Checking required packages...")
    
    for package_name, install_name in required_packages:
        if importlib.util.find_spec(package_name) is None:
            if install_name:  # Only add to missing if it needs to be installed
                missing_packages.append((package_name, install_name))
                print(f"  ❌ {package_name} - Missing")
            else:
                print(f"  ❓ {package_name} - Built-in module (not found, but should be available)")
        else:
            print(f"  ✅ {package_name} - Available")
    
    if missing_packages:
        print(f"\nInstalling {len(missing_packages)} missing package(s)...")
        for package_name, install_name in missing_packages:
            try:
                print(f"  Installing {install_name}...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", install_name])
                print(f"  ✅ {install_name} installed successfully")
            except subprocess.CalledProcessError as e:
                print(f"  ❌ Failed to install {install_name}: {e}")
                return False
        print("All packages installed successfully!\n")
    else:
        print("All required packages are already installed!\n")
    
    return True

def check_ssl_certificates():
    """Check if SSL certificates exist, generate them if they don't"""
    cert_file = "server.crt"
    key_file = "server.key"
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        print("✅ SSL certificates found")
        return True
    else:
        print("❌ SSL certificates not found")
        print("Generating new SSL certificates...")
        
        try:
            # Import and run the certificate generation script
            spec = importlib.util.spec_from_file_location("generate_cert", "generate_cert.py")
            generate_cert = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generate_cert)
            
            # Call the certificate generation function
            generate_cert.generate_self_signed_cert()
            
            if os.path.exists(cert_file) and os.path.exists(key_file):
                print("✅ SSL certificates generated successfully")
                return True
            else:
                print("❌ Failed to generate SSL certificates")
                return False
        except Exception as e:
            print(f"❌ Error generating SSL certificates: {e}")
            return False

def get_local_ip():
    """Get the local IP address"""
    try:
        import socket
        # Connect to a remote address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def run_both_servers():
    """Run both servers as separate processes"""
    local_ip = get_local_ip()
    
    print("=" * 50)
    print("LAN Voice Call Servers")
    print("=" * 50)
    print(f"HTTPS Server: https://{local_ip}:8443")
    print(f"Signaling Server: ws://{local_ip}:8765")
    print(f"Local HTTPS access: https://localhost:8443")
    print(f"Local Signaling access: ws://localhost:8765")
    print("")
    print("IMPORTANT:")
    print("- Your browser will show a security warning because this is a self-signed certificate")
    print("- This is normal for development. Click 'Advanced' and 'Proceed to ...' to continue")
    print("- All devices on the same network can access these URLs")
    print("- Media device access (microphone) will work properly with HTTPS")
    print("")
    print("Press Ctrl+C to stop both servers")
    print("=" * 50)
    
    # Start both servers as separate processes
    https_process = subprocess.Popen([sys.executable, "https_server.py"])
    # Add a small delay to ensure the first process starts
    time.sleep(1)
    signaling_process = subprocess.Popen([sys.executable, "signaling_server.py"])
    
    def signal_handler(sig, frame):
        print("\nStopping both servers...")
        https_process.terminate()
        signaling_process.terminate()
        try:
            https_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            https_process.kill()
        try:
            signaling_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            signaling_process.kill()
        print("Both servers stopped.")
        sys.exit(0)
    
    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Wait for both processes
        while True:
            if https_process.poll() is not None:
                print("HTTPS server has stopped.")
                signaling_process.terminate()
                break
            if signaling_process.poll() is not None:
                print("Signaling server has stopped.")
                https_process.terminate()
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    
    # If we get here, terminate both processes
    https_process.terminate()
    signaling_process.terminate()
    try:
        https_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        https_process.kill()
    try:
        signaling_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        signaling_process.kill()
    print("Both servers stopped.")

def main():
    """Main function"""
    print("LAN Voice Call Server Starter")
    print("=" * 30)
    
    # Step 1: Check and install packages
    if not check_and_install_packages():
        print("Failed to install required packages. Exiting.")
        return 1
    
    # Step 2: Check SSL certificates
    if not check_ssl_certificates():
        print("Failed to generate SSL certificates. Exiting.")
        return 1
    
    # Step 3: Run both servers
    run_both_servers()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())