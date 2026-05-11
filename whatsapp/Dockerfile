FROM node:20-slim

# Baileys needs python/g++ to compile some optional native modules
RUN apt-get update && apt-get install -y \
    python3 make g++ \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

COPY . .

CMD ["node", "index.js"]
