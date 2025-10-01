// Generate a random user ID
const userId = 'user-' + Math.random().toString(36).substr(2, 9);

// DOM Elements
const deviceNameElement = document.getElementById('device-name');
const editNameBtn = document.getElementById('edit-name-btn');
const editNameInput = document.getElementById('edit-name-input');
const lanIpElement = document.getElementById('lan-ip');
const peersListElement = document.getElementById('peers-list');
const incomingCallElement = document.getElementById('incoming-call');
const callerNameDisplay = document.getElementById('caller-name-display');
const acceptCallButton = document.getElementById('accept-call-large');
const rejectCallButton = document.getElementById('reject-call-large');
const callControlsElement = document.getElementById('call-controls');
const currentPeerElement = document.getElementById('current-peer');
const hangupButton = document.getElementById('hangup');
const toggleMuteButton = document.getElementById('toggle-mute');
const muteStatusElement = document.getElementById('mute-status');
const remoteAudioElement = document.getElementById('remote-audio');

// Web Audio API for ringtone
let audioContext = null;
let ringtoneOscillator = null;
let ringtoneGainNode = null;
let ringtoneInterval = null;

// Get device name (try to get actual device name, fallback to generated name)
function getDeviceName() {
    // Try to get the actual device name
    const userAgent = navigator.userAgent;
    let deviceName = "Unknown Device";
    
    // Check for common device patterns
    if (userAgent.includes("Windows")) {
        deviceName = "Windows PC";
    } else if (userAgent.includes("Mac OS")) {
        deviceName = "Mac";
    } else if (userAgent.includes("Android")) {
        deviceName = "Android Device";
    } else if (userAgent.includes("iPhone")) {
        deviceName = "iPhone";
    } else if (userAgent.includes("iPad")) {
        deviceName = "iPad";
    } else if (userAgent.includes("Linux")) {
        deviceName = "Linux PC";
    }
    
    // Add a random suffix to make it unique
    const randomSuffix = Math.floor(Math.random() * 1000);
    return `${deviceName} ${randomSuffix}`;
}

// Initialize device name
let deviceName = getDeviceName();
let isEditingName = false;

// Display device name
deviceNameElement.textContent = deviceName;

// Make device name editable
editNameBtn.addEventListener('click', () => {
    if (isEditingName) {
        // Save the new name
        deviceName = editNameInput.value.trim() || deviceName;
        deviceNameElement.textContent = deviceName;
        editNameInput.classList.add('hidden');
        deviceNameElement.classList.remove('hidden');
        isEditingName = false;
        
        // If we're connected, update our name on the server
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
                type: 'update_name',
                user_id: userId,
                name: deviceName
            }));
        }
    } else {
        // Start editing
        editNameInput.value = deviceName;
        deviceNameElement.classList.add('hidden');
        editNameInput.classList.remove('hidden');
        editNameInput.focus();
        isEditingName = true;
    }
});

// Save name when pressing Enter
editNameInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        editNameBtn.click();
    }
});

// Cancel editing when clicking outside
document.addEventListener('click', (e) => {
    if (isEditingName && 
        e.target !== editNameInput && 
        e.target !== editNameBtn && 
        !editNameInput.contains(e.target)) {
        editNameInput.classList.add('hidden');
        deviceNameElement.classList.remove('hidden');
        isEditingName = false;
    }
});

// Display LAN IP with mobile-specific instructions
function displayLanIp() {
    // Get the hostname (should be the LAN IP)
    const lanIp = window.location.hostname;
    const isLocalhost = lanIp === 'localhost' || lanIp === '127.0.0.1';
    const isHttps = window.location.protocol === 'https:';
    
    if (isLocalhost) {
        // If accessing via localhost, show both options
        lanIpElement.innerHTML = `
            <div class="font-mono">${window.location.protocol}//${lanIp}:${window.location.port}</div>
            <div class="text-sm text-gray-400 mt-2">Share with others: <span class="font-mono">${window.location.protocol}//${getLocalIP()}:${window.location.port}</span></div>
        `;
    } else {
        // If accessing via IP, show IP address
        lanIpElement.innerHTML = `
            <div class="font-mono">${window.location.href}</div>
            <div class="text-sm text-gray-400 mt-2">${isMobileDevice() ? '📱' : '💻'} Accessing from ${isMobileDevice() ? 'mobile' : 'computer'}</div>
            ${isHttps ? '<div class="text-xs mt-2 text-green-400">🔒 Secure connection</div>' : ''}
        `;
    }
}

// Get local IP address (fallback method)
function getLocalIP() {
    // This is a fallback method - in a real app, you'd get this from the server
    // For now, we'll just return the hostname
    return window.location.hostname;
}

displayLanIp();

// WebRTC Variables
let localStream = null;
let peerConnection = null;
let currentPeerId = null;
let isMicMuted = false;

// Check if device is mobile
function isMobileDevice() {
    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
}

// Enhanced media device handling with fallbacks and better error management
// Check if media devices are available and provide detailed diagnostics
function checkMediaDevices() {
    // Check if navigator exists
    if (!navigator) {
        alert('Your browser environment does not support navigation features needed for this application.');
        return false;
    }
    
    // Try multiple approaches for media device access
    // Approach 1: Standard modern API
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        return true;
    }
    
    // Approach 2: Legacy getUserMedia (for older browsers)
    if (navigator.getUserMedia) {
        return true;
    }
    
    // Approach 3: Vendor prefixed versions
    if (navigator.webkitGetUserMedia || navigator.mozGetUserMedia || navigator.msGetUserMedia) {
        return true;
    }
    
    // If none of the above work, check why
    // Provide user feedback through alerts instead of status messages
    if (!window.isSecureContext && window.location.protocol !== 'http:') {
        alert('This application requires a secure context (HTTPS) to access media devices. Please use HTTPS or localhost.');
    } else if (window.location.protocol === 'http:' && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
        alert('For security reasons, browsers only allow media device access on HTTPS or localhost. Please access this application via HTTPS or on localhost.');
    } else if (isMobileDevice()) {
        alert('Your mobile browser may have restrictions on media device access. Please:\n\n1. Ensure you are using the latest version of Chrome, Firefox, or Safari\n2. Check that you have granted microphone permissions\n3. Try accessing from a desktop browser if issues persist');
    } else {
        alert('Your browser does not support accessing media devices (microphone). Please try a modern browser like Chrome, Firefox, or Edge.');
    }
    return false;
}

// Enhanced media access with multiple fallback methods
async function getMediaStream(constraints = { audio: true, video: false }) {
    // Approach 1: Modern promise-based API
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        try {
            return await navigator.mediaDevices.getUserMedia(constraints);
        } catch (error) {
            // Handle error silently
        }
    }
    
    // Approach 2: Legacy callback-based API with promisification
    if (navigator.getUserMedia) {
        try {
            return await new Promise((resolve, reject) => {
                navigator.getUserMedia(constraints, resolve, reject);
            });
        } catch (error) {
            // Handle error silently
        }
    }
    
    // Approach 3: Vendor prefixed versions
    const vendorGetUserMedia = navigator.webkitGetUserMedia || navigator.mozGetUserMedia || navigator.msGetUserMedia;
    if (vendorGetUserMedia) {
        try {
            return await new Promise((resolve, reject) => {
                vendorGetUserMedia.call(navigator, constraints, resolve, reject);
            });
        } catch (error) {
            // Handle error silently
        }
    }
    
    // If all approaches fail, throw an error
    throw new Error('Unable to access media devices with any available method');
}

// Enhanced media access test with detailed feedback
async function testMediaAccess() {
    try {
        // Test with different constraints to find what works
        const testConstraints = [
            { audio: true, video: false }, // Audio only
            { audio: { echoCancellation: true }, video: false }, // Audio with echo cancellation
        ];
        
        for (let i = 0; i < testConstraints.length; i++) {
            try {
                const stream = await getMediaStream(testConstraints[i]);
                
                // Show only the success message you want
                alert(`Microphone access test successful!\n\nUsing constraints: ${JSON.stringify(testConstraints[i], null, 2)}`);
                
                // Stop all tracks immediately
                stream.getTracks().forEach(track => {
                    try {
                        track.stop();
                    } catch (e) {
                        // Handle error silently
                    }
                });
                
                return true;
            } catch (error) {
                if (i === testConstraints.length - 1) {
                    // Last constraint set failed, throw the error
                    throw error;
                }
            }
        }
    } catch (error) {
        // Provide detailed error feedback
        if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
            if (isMobileDevice()) {
                alert('Microphone access was denied. On mobile devices, you may need to:\n\n1. Check site permissions in your browser settings\n2. Reload the page and allow microphone access when prompted\n3. Ensure no other app is using the microphone\n4. Try using Chrome or Firefox for better compatibility');
            } else {
                alert('Microphone access was denied. Please:\n\n1. Allow microphone access when prompted\n2. Check browser permissions for this site\n3. Ensure no other application is using the microphone');
            }
        } else if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
            alert('No microphone was found. Please:\n\n1. Connect a microphone to your device\n2. Check that your microphone is properly plugged in\n3. Ensure your microphone is not disabled in system settings');
        } else if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
            alert('Could not access the microphone. Please:\n\n1. Check that no other application is using the microphone\n2. Restart your browser\n3. Check your microphone settings');
        } else if (error.name === 'OverconstrainedError') {
            alert('Your microphone does not support the required constraints. This is unusual but can happen with very basic microphones.');
        } else {
            alert(`Could not access the microphone:

${error.message}

Please check your browser console for more details.`);
        }
        return false;
    }
}

// Solution 3: WebSocket connection with HTTPS/WSS support
// Use the same hostname as the web page but with port 8765 for WebSocket
const isHttps = window.location.protocol === 'https:';
const wsProtocol = isHttps ? 'wss://' : 'ws://';
const wsHost = window.location.hostname;
const wsPort = '8765';
const wsUrl = `${wsProtocol}${wsHost}:${wsPort}`;

let socket = null;
let reconnectAttempts = 0;
const maxReconnectAttempts = 5;

function connectWebSocket() {
    try {
        socket = new WebSocket(wsUrl);
        
        // WebSocket event handlers
        socket.onopen = function(event) {
            reconnectAttempts = 0; // Reset reconnect attempts on successful connection
            
            // Register with the signaling server
            const registerMessage = {
                type: 'register',
                user_id: userId,
                name: deviceName
            };
            socket.send(JSON.stringify(registerMessage));
        };

        socket.onmessage = async function(event) {
            const message = JSON.parse(event.data);
            
            switch(message.type) {
                case 'client_list':
                    updatePeersList(message.clients);
                    break;
                case 'offer':
                    handleOffer(message);
                    break;
                case 'answer':
                    handleAnswer(message);
                    break;
                case 'ice_candidate':
                    handleIceCandidate(message);
                    break;
                case 'hangup':
                    handleHangup(message);
                    break;
            }
        };

        socket.onerror = function(error) {
            // No visual indication of connection status since we removed the status elements
        };

        socket.onclose = function(event) {
            // No visual indication of connection status since we removed the status elements
            
            // Attempt to reconnect if not max attempts reached
            if (reconnectAttempts < maxReconnectAttempts) {
                reconnectAttempts++;
                setTimeout(connectWebSocket, 2000 * reconnectAttempts); // Exponential backoff
            }
        };
    } catch (error) {
        // No visual indication of connection status since we removed the status elements
    }
}

// Initial connection
connectWebSocket();

// Enhanced ICE servers configuration with multiple options
const iceServers = [
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'stun:stun1.l.google.com:19302' },
    { urls: 'stun:stun.stunprotocol.org:3478' },
    { urls: 'stun:stun.voiparound.com' },
    { urls: 'stun:stun.voipbuster.com' }
];

// Create RTCPeerConnection with better configuration
function createPeerConnection() {
    try {
        // Enhanced configuration for better compatibility
        const config = {
            iceServers: iceServers,
            iceCandidatePoolSize: 10
        };
        
        peerConnection = new RTCPeerConnection(config);
        
        // Handle ICE candidates
        peerConnection.onicecandidate = event => {
            if (event.candidate) {
                socket.send(JSON.stringify({
                    type: 'ice_candidate',
                    target_id: currentPeerId,
                    candidate: event.candidate
                }));
            }
        };
        
        // Handle incoming tracks with better audio handling
        peerConnection.ontrack = event => {
            // Set the remote stream to the audio element
            remoteAudioElement.srcObject = event.streams[0];
            
            // Ensure the audio element is configured properly
            remoteAudioElement.autoplay = true;
            remoteAudioElement.playsInline = true; // Important for mobile Safari
            
            // Try to play the audio (needed for some mobile browsers)
            remoteAudioElement.play().catch(error => {
                // Show a user gesture prompt if needed
                alert('Please tap anywhere on the screen to enable audio playback');
            });
        };
        
        // Handle connection state changes
        peerConnection.onconnectionstatechange = () => {
            if (peerConnection.connectionState === 'disconnected' || 
                peerConnection.connectionState === 'failed') {
                endCall();
            }
        };
        
        // Handle ICE connection state changes
        peerConnection.oniceconnectionstatechange = () => {
            if (peerConnection.iceConnectionState === 'disconnected' || 
                peerConnection.iceConnectionState === 'failed') {
                endCall();
            }
        };
    } catch (error) {
        throw new Error('Failed to create peer connection: ' + error.message);
    }
}

// Function to play ringtone using Web Audio API
function playRingtone() {
    try {
        // Create audio context if it doesn't exist
        if (!audioContext) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        
        // Resume audio context if it's suspended (needed for autoplay policies)
        if (audioContext.state === 'suspended') {
            audioContext.resume();
        }
        
        // Create oscillator for ringtone
        ringtoneOscillator = audioContext.createOscillator();
        ringtoneGainNode = audioContext.createGain();
        
        // Configure oscillator for a phone-like ringtone
        ringtoneOscillator.type = 'sine';
        ringtoneOscillator.frequency.setValueAtTime(800, audioContext.currentTime);
        
        // Configure gain for volume control
        ringtoneGainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
        
        // Connect nodes
        ringtoneOscillator.connect(ringtoneGainNode);
        ringtoneGainNode.connect(audioContext.destination);
        
        // Start oscillator
        ringtoneOscillator.start();
        
        // Create ringing pattern (2 seconds on, 4 seconds off)
        let isPlaying = true;
        ringtoneInterval = setInterval(() => {
            if (isPlaying) {
                ringtoneGainNode.gain.setValueAtTime(0, audioContext.currentTime);
                isPlaying = false;
            } else {
                ringtoneGainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
                isPlaying = true;
            }
        }, 2000);
        
        console.log("Ringtone started");
    } catch (error) {
        console.log("Could not play ringtone:", error);
    }
}

// Function to stop ringtone
function stopRingtone() {
    try {
        if (ringtoneOscillator) {
            ringtoneOscillator.stop();
            ringtoneOscillator = null;
        }
        
        if (ringtoneInterval) {
            clearInterval(ringtoneInterval);
            ringtoneInterval = null;
        }
        
        if (ringtoneGainNode) {
            ringtoneGainNode.disconnect();
            ringtoneGainNode = null;
        }
        
        console.log("Ringtone stopped");
    } catch (error) {
        console.log("Error stopping ringtone:", error);
    }
}

// Start a call with a peer
async function startCall(peerId) {
    try {
        // Check if media devices are available
        if (!checkMediaDevices()) {
            return;
        }
        
        // Test media access before proceeding
        if (!(await testMediaAccess())) {
            return;
        }
        
        currentPeerId = peerId;
        currentPeerElement.textContent = peerId;
        
        // Get local media stream with specific constraints for better compatibility
        const constraints = {
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            },
            video: false
        };
        
        localStream = await getMediaStream(constraints);
        
        // Create peer connection
        createPeerConnection();
        
        // Add local stream to peer connection
        localStream.getTracks().forEach(track => {
            peerConnection.addTrack(track, localStream);
        });
        
        // Create offer with specific configuration for better compatibility
        const offerOptions = {
            offerToReceiveAudio: true,
            offerToReceiveVideo: false,
            voiceActivityDetection: true
        };
        
        const offer = await peerConnection.createOffer(offerOptions);
        
        await peerConnection.setLocalDescription(offer);
        
        // Send offer to peer via signaling server
        socket.send(JSON.stringify({
            type: 'offer',
            target_id: peerId,
            offer: offer
        }));
        
        // Show call controls
        callControlsElement.classList.remove('hidden');
        
    } catch (error) {
        alert('Failed to start call: ' + error.message);
        
        // Clean up on error
        endCall();
    }
}

// Handle incoming offer
async function handleOffer(message) {
    try {
        // Check if media devices are available
        if (!checkMediaDevices()) {
            // Send hangup to reject the call
            socket.send(JSON.stringify({
                type: 'hangup',
                target_id: message.sender_id
            }));
            return;
        }
        
        currentPeerId = message.sender_id;
        
        // Display the caller's name (use the name if available, otherwise use the ID)
        const callerName = message.sender_name || message.sender_id;
        callerNameDisplay.textContent = callerName;
        
        // Show incoming call notification
        incomingCallElement.classList.remove('hidden');
        
        // Play ringtone
        playRingtone();
        
        // Set up event listeners for accept/reject buttons
        acceptCallButton.onclick = async () => {
            // Stop ringtone
            stopRingtone();
            
            incomingCallElement.classList.add('hidden');
            
            try {
                // Test media access before proceeding
                if (!(await testMediaAccess())) {
                    // Send hangup to reject the call
                    socket.send(JSON.stringify({
                        type: 'hangup',
                        target_id: message.sender_id
                    }));
                    endCall();
                    return;
                }
                
                // Get local media stream with specific constraints
                const constraints = {
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true
                    },
                    video: false
                };
                
                localStream = await getMediaStream(constraints);
                
                // Create peer connection
                createPeerConnection();
                
                // Add local stream to peer connection
                localStream.getTracks().forEach(track => {
                    peerConnection.addTrack(track, localStream);
                });
                
                // Set remote description
                await peerConnection.setRemoteDescription(message.offer);
                
                // Create answer with specific configuration
                const answerOptions = {
                    voiceActivityDetection: true
                };
                
                const answer = await peerConnection.createAnswer(answerOptions);
                
                await peerConnection.setLocalDescription(answer);
                
                // Send answer to peer via signaling server
                socket.send(JSON.stringify({
                    type: 'answer',
                    target_id: message.sender_id,
                    answer: answer
                }));
                
                // Show call controls
                callControlsElement.classList.remove('hidden');
                currentPeerElement.textContent = callerName;
                
            } catch (error) {
                // Stop ringtone
                stopRingtone();
                
                alert('Failed to accept call: ' + error.message);
                
                // Send hangup to reject the call
                socket.send(JSON.stringify({
                    type: 'hangup',
                    target_id: message.sender_id
                }));
                
                // Clean up on error
                endCall();
            }
        };
        
        rejectCallButton.onclick = () => {
            // Stop ringtone
            stopRingtone();
            
            incomingCallElement.classList.add('hidden');
            // Send hangup message to reject the call
            socket.send(JSON.stringify({
                type: 'hangup',
                target_id: message.sender_id
            }));
            endCall();
        };
        
    } catch (error) {
        // Stop ringtone if there's an error
        stopRingtone();
        
        alert('Error handling call: ' + error.message);
    }
}

// Handle incoming answer
async function handleAnswer(message) {
    try {
        await peerConnection.setRemoteDescription(message.answer);
    } catch (error) {
        alert('Error handling answer: ' + error.message);
    }
}

// Handle incoming ICE candidate
async function handleIceCandidate(message) {
    try {
        if (peerConnection) {
            await peerConnection.addIceCandidate(message.candidate);
        }
    } catch (error) {
        // Handle error silently
    }
}

// Handle hangup
function handleHangup(message) {
    // Stop ringtone if it's playing
    stopRingtone();
    
    endCall();
}

// End the current call
function endCall() {
    // Hide call UI
    callControlsElement.classList.add('hidden');
    incomingCallElement.classList.add('hidden');
    
    // Stop ringtone if it's playing
    stopRingtone();
    
    // Stop local stream tracks
    if (localStream) {
        localStream.getTracks().forEach(track => {
            try {
                track.stop();
            } catch (e) {
                // Handle error silently
            }
        });
        localStream = null;
    }
    
    // Close peer connection
    if (peerConnection) {
        try {
            peerConnection.close();
        } catch (e) {
            // Handle error silently
        }
        peerConnection = null;
    }
    
    currentPeerId = null;
}

// Hang up the current call
function hangUp() {
    if (currentPeerId) {
        socket.send(JSON.stringify({
            type: 'hangup',
            target_id: currentPeerId
        }));
    }
    endCall();
}

// Toggle microphone mute
function toggleMute() {
    if (localStream) {
        isMicMuted = !isMicMuted;
        localStream.getAudioTracks().forEach(track => {
            track.enabled = !isMicMuted;
        });
        
        if (isMicMuted) {
            toggleMuteButton.innerHTML = '<span>🎤</span> Unmute Microphone';
            muteStatusElement.textContent = 'Microphone: Off';
        } else {
            toggleMuteButton.innerHTML = '<span>🎤</span> Mute Microphone';
            muteStatusElement.textContent = 'Microphone: On';
        }
    }
}

// Update the peers list in the UI
function updatePeersList(clients) {
    // Filter out our own ID
    const peers = clients.filter(client => client.id !== userId);
    
    if (peers.length === 0) {
        peersListElement.innerHTML = `
            <div class="empty-state">
                <p>No peers connected yet</p>
            </div>
        `;
        return;
    }
    
    peersListElement.innerHTML = '';
    
    peers.forEach(peer => {
        const peerElement = document.createElement('div');
        peerElement.className = 'peer-item';
        peerElement.innerHTML = `
            <div class="peer-info">
                <div class="peer-name">${peer.name || peer.id}</div>
                <div class="peer-status">
                    <span class="status-indicator status-online"></span>
                    <span>Online</span>
                </div>
            </div>
            <div class="peer-actions">
                <button class="btn btn-primary call-button" data-peer-id="${peer.id}">
                    <span>📞</span> Call
                </button>
            </div>
        `;
        peersListElement.appendChild(peerElement);
    });
    
    // Add event listeners to call buttons
    document.querySelectorAll('.call-button').forEach(button => {
        button.addEventListener('click', () => {
            const peerId = button.getAttribute('data-peer-id');
            startCall(peerId);
        });
    });
}

// Event listeners
hangupButton.addEventListener('click', hangUp);
toggleMuteButton.addEventListener('click', toggleMute);

// Handle page visibility changes to detect when user leaves
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
        // Send hangup if in a call when leaving the page
        if (currentPeerId) {
            hangUp();
        }
    }
});

// Handle beforeunload to send hangup
window.addEventListener('beforeunload', () => {
    if (currentPeerId) {
        hangUp();
    }
});

// Add a click event listener to the document to enable audio playback on mobile
document.addEventListener('click', function() {
    if (remoteAudioElement && remoteAudioElement.srcObject) {
        remoteAudioElement.play().catch(error => {
            // Handle error silently
        });
    }
    
    // Resume audio context on first click (to overcome autoplay restrictions)
    if (audioContext && audioContext.state === 'suspended') {
        audioContext.resume();
    }
}, { once: true });

// Add additional click listener for ringtone (in case the first one doesn't work)
document.addEventListener('click', function() {
    // Resume audio context on click (to overcome autoplay restrictions)
    if (audioContext && audioContext.state === 'suspended') {
        audioContext.resume();
    }
});