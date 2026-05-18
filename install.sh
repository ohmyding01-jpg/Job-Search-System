#!/bin/bash
# Job Agent — One-time setup for Stephen's machine
# Run once: bash install.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "========================================================"
echo "  Job Agent Setup"
echo "========================================================"
echo ""

# 1. Python check
if ! command -v python3 &>/dev/null; then
    echo "Python3 not found. Installing via Homebrew..."
    if ! command -v brew &>/dev/null; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    brew install python3
fi
PYTHON=$(command -v python3)
echo "✓ Python: $($PYTHON --version)"

# 2. pip dependencies
echo "Installing Python packages..."
$PYTHON -m pip install -q --upgrade pip
$PYTHON -m pip install -q \
    playwright fastapi "uvicorn[standard]" \
    python-dotenv rich apscheduler \
    anthropic google-genai httpx pyyaml aiofiles

# 3. Playwright browsers
echo "Installing Playwright browser (Chromium)..."
$PYTHON -m playwright install chromium

# 4. Create .env if missing
if [ ! -f ".env" ]; then
    cp .env.example .env 2>/dev/null || cat > .env << 'EOF'
# Job Agent — API Keys
# Get a free Google AI key at https://aistudio.google.com/apikey
# Then paste it in the dashboard (http://localhost:8080) or here:
GEMINI_API_KEY=""
EOF
    echo "✓ Created .env file (add your API key in the dashboard)"
fi

# 5. Create the candidate profile directories
mkdir -p "candidates/stephen/session"
mkdir -p "candidates/stephen/data"
mkdir -p "candidates/stephen/documents/resumes"
mkdir -p "candidates/stephen/documents/cover_letters"
mkdir -p "logs"

# 6. Make start.py executable
chmod +x start.py 2>/dev/null || true

echo ""
echo "========================================================"
echo "  Setup complete!"
echo ""
echo "  To start the job agent:"
echo "    python3 start.py"
echo ""
echo "  A browser will open at http://localhost:8080"
echo "  Enter your free Google AI key there to begin."
echo "  Get one at: https://aistudio.google.com/apikey"
echo "========================================================"
echo ""
