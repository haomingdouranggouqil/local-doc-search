FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json /app/
RUN npm ci
COPY index.html /app/index.html
COPY src /app/src
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
