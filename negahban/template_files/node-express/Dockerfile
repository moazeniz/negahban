FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build --if-present && npm prune --omit=dev
ENV NODE_ENV=production PORT={{port}}
EXPOSE {{port}}
CMD ["npm", "start"]
