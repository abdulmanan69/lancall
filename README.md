# LAN Voice Call Application

A peer-to-peer voice calling application that works over LAN (Local Area Network) using WebRTC technology. This application allows users on the same network to make voice calls directly between devices without any external servers.

## Repository

- **GitHub**: [LAN Voice Call on GitHub](https://github.com/yourusername/lan-voice-call)

## Features

- **Peer-to-Peer Communication**: Direct voice communication between devices using WebRTC
- **LAN Only**: Works exclusively on local networks for privacy and security
- **Cross-Platform**: Compatible with Windows, macOS, Linux, Android, and iOS browsers
- **No External Dependencies**: All communication happens directly between devices
- **Secure**: Uses HTTPS and WSS (WebSocket Secure) with self-signed certificates
- **Easy Setup**: Simple installation and startup process

## How It Works

The application uses WebRTC (Web Real-Time Communication) technology to establish direct peer-to-peer connections between devices on the same network. It consists of two main components:

1. **HTTPS Server**: Serves the web application files (HTML, CSS, JavaScript)
2. **WebSocket Signaling Server**: Facilitates the initial connection setup between peers

### Architecture

```
[Device A] ----\
                \
                 [Signaling Server] <---> [HTTPS Server]
                /
[Device B] ----/
```

1. Devices connect to the signaling server to exchange connection information
2. Once connection details are exchanged, devices establish a direct P2P connection
3. Voice data flows directly between devices (not through the server)

## Prerequisites

- Python 3.6 or higher
- pip (Python package installer)

## Installation

1. Clone or download this repository
2. Navigate to the project directory
3. Run the startup script:

### Windows
```cmd
start_server.bat
```

### Linux/macOS/Other
```bash
python start_server.py
```

The script will automatically:
- Install required Python packages (pyOpenSSL, websockets)
- Generate self-signed SSL certificates if they don't exist
- Start both the HTTPS server (port 8443) and WebSocket signaling server (port 8765)

## Usage

1. Run the startup script on one device (this will be your "server" device)
2. Note the IP address displayed in the terminal
3. On the same device or another device on the same network, open a browser and navigate to:
   ```
   https://[SERVER_IP]:8443
   ```
4. Accept the browser security warning (self-signed certificate)
5. Allow microphone access when prompted
6. Other devices on the same network can join by visiting the same URL
7. Click on other devices in the list to initiate a voice call

## Project Structure

```
lan-voice-call/
├── index.html          # Main HTML file
├── style.css           # Styling
├── app.js              # Client-side JavaScript application
├── https_server.py     # HTTPS server for serving web files
├── signaling_server.py # WebSocket signaling server
├── start_server.py     # Python startup script
├── start_server.bat    # Windows batch startup script
├── generate_cert.py    # SSL certificate generator
├── server.crt          # SSL certificate (generated)
├── server.key          # SSL private key (generated)
└── README.md           # This file
```

## Security Notes

- Uses self-signed SSL certificates for HTTPS and WSS
- Browsers will show security warnings - this is expected for self-signed certificates
- All communication is encrypted
- No data leaves your local network
- Microphone access is only used for the voice call feature

## Troubleshooting

### Certificate Errors
If you encounter certificate errors:
1. Make sure you're accessing the correct IP address
2. Accept the browser security warning
3. If problems persist, delete `server.crt` and `server.key` files and restart the server

### Connection Issues
- Ensure all devices are on the same network
- Check firewall settings
- Make sure ports 8443 (HTTPS) and 8765 (WebSocket) are not blocked

### Audio Issues
- Check browser microphone permissions
- Ensure no other applications are using the microphone
- Try refreshing the page

## Dependencies

- Python 3.6+
- pyOpenSSL
- websockets

These are automatically installed by the startup scripts.

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Acknowledgments

- WebRTC for enabling real-time communication
- Python for the server implementation
- All contributors to the open-source libraries used in this project