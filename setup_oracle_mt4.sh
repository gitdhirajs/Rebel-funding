#!/bin/bash
# setup_oracle_mt4.sh
# Run this script on your Oracle Cloud Ubuntu server to prepare it for MetaTrader 4

echo "Installing Desktop Environment and Wine for MetaTrader 4..."

# Update system
sudo apt update && sudo apt upgrade -y

# Install a lightweight desktop environment (XFCE)
sudo DEBIAN_FRONTEND=noninteractive apt install xfce4 xfce4-goodies -y

# Install TightVNCServer to allow remote desktop connection
sudo apt install tightvncserver -y

# Add 32-bit architecture support for Wine (MT4 requires it)
sudo dpkg --add-architecture i386

# Download and add Wine repository key
sudo mkdir -pm755 /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
sudo wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources

# Install Wine
sudo apt update
sudo apt install --install-recommends winehq-stable -y

# Configure VNC
echo "Configuring VNC Server..."
mkdir -p ~/.vnc

cat << 'EOF' > ~/.vnc/xstartup
#!/bin/bash
xrdb $HOME/.Xresources
startxfce4 &
EOF

chmod +x ~/.vnc/xstartup

echo ""
echo "=========================================================="
echo "SETUP COMPLETE!"
echo "Next steps:"
echo "1. Run the command: vncserver"
echo "   (It will ask you to create a password for remote access)"
echo "2. Open port 5901 in your Oracle Cloud Firewall / Security List"
echo "3. Connect using a VNC Viewer to your server IP: <Your-IP>:5901"
echo "4. Once connected, download MT4 inside the VNC desktop:"
echo "   wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt4/mt4setup.exe"
echo "   wine mt4setup.exe"
echo "=========================================================="
