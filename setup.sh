#!/bin/bash
# LinkedIn Job Agent — First-Time Setup
set -e

echo "=== LinkedIn Job Agent Setup ==="
echo ""

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python: $python_version"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate
source .venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Install Playwright browsers
echo "Installing Playwright browser (Chromium)..."
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

# Create required directories
mkdir -p data session output/resumes output/cover_letters logs

# Copy .env if not exists
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo ">>> .env created. Open it and fill in:"
    echo "    ANTHROPIC_API_KEY=..."
    echo "    LINKEDIN_EMAIL=..."
    echo "    LINKEDIN_PASSWORD=..."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your credentials"
echo "  2. Edit resume_profile.yaml with Samiha's real details"
echo "  3. Run: source .venv/bin/activate && python main.py"
