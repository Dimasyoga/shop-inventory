#!/bin/bash
# Shop Inventory System - Startup Script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/home/phoe/.local/bin:$PATH"

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv --without-pip
    echo "Bootstrapping pip..."
    curl -sS https://bootstrap.pypa.io/get-pip.py | venv/bin/python3
    echo ""
fi

# Install dependencies
echo "Installing dependencies..."
venv/bin/python3 -m pip install -r requirements.txt
echo ""

echo "================================="
echo "  Shop Inventory Management System"
echo "================================="
echo ""
echo "Starting server..."
echo "Access the dashboard at: http://localhost:5000"
echo "Or from LAN: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Default login: admin / admin123"
echo "Press Ctrl+C to stop"
echo ""

# Set FLASK_DEBUG=1 to enable the interactive debugger (development only --
# it allows code execution by anyone who can reach the server).
venv/bin/python3 app.py
