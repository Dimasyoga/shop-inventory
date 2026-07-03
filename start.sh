#!/bin/bash
# Shop Inventory System - Startup Script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/home/phoe/.local/bin:$PATH"

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

python3 app.py
