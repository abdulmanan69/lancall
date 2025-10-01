#!/usr/bin/env python3
"""
Signaling server for the LAN voice call application
Handles WebSocket connections for WebRTC signaling
"""

import asyncio
import websockets
import json
import logging
import socket
import ssl
import os

# Get the LAN IP address
def get_lan_ip():
    try:
        # Connect to a remote address (doesn't actually send data)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store connected clients: {websocket: {id, name}}
connected_clients = {}

# Get the LAN IP address
lan_ip = get_lan_ip()

async def register_client(websocket, user_id, name=None):
    """Register a new client with their user ID and name"""
    connected_clients[websocket] = {
        "id": user_id,
        "name": name or user_id
    }
    logger.info(f"Client registered: {user_id} ({name or 'unnamed'})")
    
    # Send the list of all connected clients to the new client
    await send_client_list()

async def update_client_name(websocket, user_id, name):
    """Update a client's name"""
    if websocket in connected_clients:
        connected_clients[websocket]["name"] = name
        logger.info(f"Client {user_id} updated name to: {name}")
        
        # Notify all clients about the updated list
        await send_client_list()

async def unregister_client(websocket):
    """Remove a client from the connected clients list"""
    if websocket in connected_clients:
        client_info = connected_clients[websocket]
        del connected_clients[websocket]
        logger.info(f"Client unregistered: {client_info['id']} ({client_info['name']})")
        
        # Notify all remaining clients about the updated list
        await send_client_list()

async def send_client_list():
    """Send the current list of connected clients to all clients"""
    client_list = [
        {"id": client["id"], "name": client["name"]} 
        for client in connected_clients.values()
    ]
    
    message = {
        "type": "client_list",
        "clients": client_list
    }
    
    # Send to all connected clients
    if connected_clients:
        # Create a list of tasks to send messages concurrently
        tasks = []
        for client in list(connected_clients.keys()):  # Use list() to avoid "dictionary changed size during iteration"
            try:
                tasks.append(client.send(json.dumps(message)))
            except:
                # Remove disconnected clients
                if client in connected_clients:
                    del connected_clients[client]
        
        # Run all tasks concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

async def handle_message(websocket, message):
    """Handle incoming messages from clients"""
    try:
        data = json.loads(message)
        msg_type = data.get("type")
        
        logger.info(f"Received message type: {msg_type} from {connected_clients.get(websocket, {}).get('id', 'unknown')}")
        
        if msg_type == "register":
            user_id = data.get("user_id", "Anonymous")
            name = data.get("name", user_id)
            logger.info(f"Registering client: {user_id} ({name})")
            await register_client(websocket, user_id, name)
            
        elif msg_type == "update_name":
            user_id = data.get("user_id")
            name = data.get("name", user_id)
            logger.info(f"Updating name for client: {user_id} to {name}")
            await update_client_name(websocket, user_id, name)
            
        elif msg_type == "offer":
            target_id = data.get("target_id")
            sender_info = connected_clients.get(websocket, {"id": "unknown", "name": "unknown"})
            logger.info(f"Forwarding offer from {sender_info['id']} ({sender_info['name']}) to {target_id}")
            # Forward WebRTC signaling messages to the target client
            if target_id:
                # Find the target websocket
                target_socket = None
                for ws, client_info in connected_clients.items():
                    if client_info["id"] == target_id:
                        target_socket = ws
                        break
                        
                if target_socket:
                    # Add sender info to the message
                    data["sender_id"] = sender_info["id"]
                    data["sender_name"] = sender_info["name"]
                    try:
                        await target_socket.send(json.dumps(data))
                        logger.info(f"Offer forwarded successfully")
                    except Exception as e:
                        logger.error(f"Failed to send offer to {target_id}: {e}")
                    
        elif msg_type == "answer":
            target_id = data.get("target_id")
            sender_info = connected_clients.get(websocket, {"id": "unknown", "name": "unknown"})
            logger.info(f"Forwarding answer from {sender_info['id']} ({sender_info['name']}) to {target_id}")
            # Forward WebRTC signaling messages to the target client
            if target_id:
                # Find the target websocket
                target_socket = None
                for ws, client_info in connected_clients.items():
                    if client_info["id"] == target_id:
                        target_socket = ws
                        break
                        
                if target_socket:
                    # Add sender info to the message
                    data["sender_id"] = sender_info["id"]
                    data["sender_name"] = sender_info["name"]
                    try:
                        await target_socket.send(json.dumps(data))
                        logger.info(f"Answer forwarded successfully")
                    except Exception as e:
                        logger.error(f"Failed to send answer to {target_id}: {e}")
                    
        elif msg_type == "ice_candidate":
            target_id = data.get("target_id")
            sender_info = connected_clients.get(websocket, {"id": "unknown", "name": "unknown"})
            logger.info(f"Forwarding ICE candidate from {sender_info['id']} ({sender_info['name']}) to {target_id}")
            # Forward WebRTC signaling messages to the target client
            if target_id:
                # Find the target websocket
                target_socket = None
                for ws, client_info in connected_clients.items():
                    if client_info["id"] == target_id:
                        target_socket = ws
                        break
                        
                if target_socket:
                    # Add sender info to the message
                    data["sender_id"] = sender_info["id"]
                    data["sender_name"] = sender_info["name"]
                    try:
                        await target_socket.send(json.dumps(data))
                        logger.info(f"ICE candidate forwarded successfully")
                    except Exception as e:
                        logger.error(f"Failed to send ICE candidate to {target_id}: {e}")
                    
        elif msg_type == "hangup":
            # Forward hangup message to the target client
            target_id = data.get("target_id")
            sender_info = connected_clients.get(websocket, {"id": "unknown", "name": "unknown"})
            logger.info(f"Forwarding hangup from {sender_info['id']} ({sender_info['name']}) to {target_id}")
            if target_id:
                target_socket = None
                for ws, client_info in connected_clients.items():
                    if client_info["id"] == target_id:
                        target_socket = ws
                        break
                        
                if target_socket:
                    data["sender_id"] = sender_info["id"]
                    data["sender_name"] = sender_info["name"]
                    try:
                        await target_socket.send(json.dumps(data))
                        logger.info(f"Hangup forwarded successfully")
                    except Exception as e:
                        logger.error(f"Failed to send hangup to {target_id}: {e}")
                    
    except json.JSONDecodeError:
        logger.error("Invalid JSON message received")
    except Exception as e:
        logger.error(f"Error handling message: {e}")

async def handler(websocket):
    """Handle a new WebSocket connection"""
    logger.info(f"New WebSocket connection from {websocket.remote_address}")
    try:
        async for message in websocket:
            await handle_message(websocket, message)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await unregister_client(websocket)

# Main function
async def main():
    """Main function to start the server"""
    # Check if we should use SSL
    use_ssl = os.path.exists("server.crt") and os.path.exists("server.key")
    
    if use_ssl:
        # Set up SSL context
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain("server.crt", "server.key")
        
        # For websockets v15, we pass the handler directly with SSL
        server = await websockets.serve(handler, "0.0.0.0", 8765, ssl=ssl_context)
        logger.info(f"Secure signaling server started on wss://{lan_ip}:8765")
        logger.info(f"Accessible locally at wss://localhost:8765")
        print(f"Secure signaling server started on wss://{lan_ip}:8765")
        print(f"Accessible locally at wss://localhost:8765")
    else:
        # For websockets v15, we pass the handler directly
        server = await websockets.serve(handler, "0.0.0.0", 8765)
        logger.info(f"Signaling server started on ws://{lan_ip}:8765")
        logger.info(f"Accessible locally at ws://localhost:8765")
        print(f"Signaling server started on ws://{lan_ip}:8765")
        print(f"Accessible locally at ws://localhost:8765")
    
    try:
        await server.wait_closed()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")