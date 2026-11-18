# Frontend image (local deployment). Runs the Vite server so VITE_* values are read from the
# container environment at start-up (env_file) instead of being compiled into the image.
FROM node:22-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY index.html vite.config.js ./
COPY src ./src

USER node
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
