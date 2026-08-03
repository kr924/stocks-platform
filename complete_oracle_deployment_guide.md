# 📘 Layman's Complete Guide: Deploying Stock AI Platform on Oracle Cloud

This guide breaks down every single concept and exact terminal command executed to deploy your **Indian Stock Market AI Platform** onto your **Oracle Cloud Server**.

---

## 🏗️ 1. High-Level Architecture (How It Works)

Think of your cloud setup as 4 components working together 24/7:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ORACLE CLOUD SERVER (VM)                          │
│                                                                             │
│  ┌───────────────────────────┐         ┌──────────────────────────────────┐ │
│  │   DOCKER CONTAINER        │         │   LOCAL OLLAMA AI ENGINE         │ │
│  │  (FastAPI + React App)    │ ──────> │  (qwen2.5:7b / stocks-analyst)   │ │
│  │   Port 8000               │         │   Port 11434                     │ │
│  └─────────────┬─────────────┘         └──────────────────────────────────┘ │
└────────────────┼────────────────────────────────────────────────────────────┘
                 │
                 ▼
 ┌──────────────────────────────┐          ┌──────────────────────────────────┐
 │  CLOUDFLARE HTTPS TUNNEL     │ ───────> │  BROWSER / UPSTOX OAUTH LOGIN    │
 │ (https://...trycloudflare.com│          │  (Real-Time Live Dashboard)      │
 └──────────────────────────────┘          └──────────────────────────────────┘
```

1. **Docker Container (`stocks-app`)**: Houses both your FastAPI Python backend (scrapers, API endpoints) and your compiled React frontend served on Port 8000.
2. **Local Ollama AI (`stocks-analyst`)**: Runs `qwen2.5:7b` directly on your server hardware as a free, 0-cost fallback AI model.
3. **Cloudflare Tunnel (`cloudflared`)**: Creates a free, secure `https://` address so Upstox OAuth security checks pass.
4. **Primary AI (`Groq API`)**: Uses Llama 3.3 70B for instant, high-speed news summaries and sentiment scoring.

---

## 🚀 2. Complete Step-by-Step Command Flow

Follow these exact steps whenever setting up a new server from scratch:

### Step 1: Connect to Server & Install System Dependencies
Open PowerShell on your Windows PC and SSH into your server:
```powershell
ssh -i $HOME\.ssh\oci_key ubuntu@129.159.23.190
```

On the server, install **Docker** (to run the app) and **Git** (to pull code):
```bash
# Update server package lists
sudo apt update

# Install Docker and Git
sudo apt install -y docker.io git

# Set Docker to start automatically when server boots
sudo systemctl enable --now docker

# Give ubuntu user permission to run docker without sudo
sudo usermod -aG docker ubuntu
```

---

### Step 2: Install & Set Up Local Ollama AI
```bash
# 1. Install Ollama AI engine
curl -fsSL https://ollama.com/install.sh | sh

# 2. Enable Ollama service auto-restart on boot
sudo systemctl enable --now ollama

# 3. Pull/Run the Qwen model
ollama run qwen2.5:3b
```

---

### Step 3: Clone Code & Configure `.env` Secrets
```bash
# 1. Clone repository from GitHub
git clone https://github.com/kr924/stocks-platform.git ~/stocks
cd ~/stocks

# 2. Create database file on server host
touch ~/stocks/backend/market_tracker.db

# 3. Create production .env configuration file
cat > ~/stocks/backend/.env << 'EOF'
PROVIDER=upstox
UPSTOX_CLIENT_ID=e8422cc2-9847-4fd6-bf2b-3eaea3f0ea15
UPSTOX_CLIENT_SECRET=a6h4vf9voo
UPSTOX_REDIRECT_URI=https://mesh-deferred-legendary-ellis.trycloudflare.com/api/auth/callback

GEMINI_API_KEY=YOUR_GEMINI_API_KEY
GROQ_API_KEY=YOUR_GROQ_API_KEY
GROQ_MODEL=llama-3.3-70b-versatile
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:3b

FRONTEND_URL=https://mesh-deferred-legendary-ellis.trycloudflare.com
DATABASE_URL=sqlite:////app/backend/market_tracker.db
EOF
```

---

### Step 4: Build & Launch Docker Container
```bash
# 1. Build Docker image (automatically compiles React frontend + Python environment)
sudo docker build -t stocks-app .

# 2. Run container in background 24/7
sudo docker run -d \
  --name stocks-app \
  --restart always \
  --network host \
  --env-file ~/stocks/backend/.env \
  -v ~/stocks/backend/market_tracker.db:/app/backend/market_tracker.db \
  stocks-app
```

---

### Step 5: Start Cloudflare HTTPS Tunnel 24/7 Background Service
```bash
# 1. Download cloudflared binary tool
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared

# 2. Start tunnel in background using nohup
nohup ./cloudflared tunnel --url http://localhost:8000 > cloudflared.log 2>&1 &

# 3. View your active HTTPS URL link
grep "trycloudflare.com" cloudflared.log
```

---

## 🔑 3. Upstox Developer Console Setup

1. Go to **[developer.upstox.com](https://developer.upstox.com)**.
2. Select your registered App (`e8422cc2-9847-4fd6-bf2b-3eaea3f0ea15`).
3. Set **Redirect URL** to match your active HTTPS link:
   ```text
   https://mesh-deferred-legendary-ellis.trycloudflare.com/api/auth/callback
   ```
4. Save changes.

---

## 🛠️ 4. Useful Management Commands

### Check if App Container is Running:
```bash
sudo docker ps
```

### View Live Backend Logs:
```bash
sudo docker logs -f stocks-app
```

### Check Local Ollama AI Status:
```bash
curl http://localhost:11434/api/tags
```

---

## 🔄 5. Future 1-Click Code Updates

Whenever you make code changes on your local machine and push to GitHub, update your live cloud server with this **single command block**:

```bash
cd ~/stocks
git pull origin main
sudo docker build -t stocks-app .
sudo docker rm -f stocks-app
sudo docker run -d \
  --name stocks-app \
  --restart always \
  --network host \
  --env-file ~/stocks/backend/.env \
  -v ~/stocks/backend/market_tracker.db:/app/backend/market_tracker.db \
  stocks-app
```
