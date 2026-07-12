# KQuery 前端服务 - Docker镜像

FROM node:18-alpine

# 设置工作目录
WORKDIR /app

# 复制项目文件
COPY package*.json ./

# 安装依赖
RUN npm install

# 复制源代码
COPY . .

# 构建项目
RUN npm run build

# 安装静态文件服务器
RUN npm install -g serve

# 暴露端口
EXPOSE 3000

# 启动命令
CMD ["serve", "-s", "dist", "-l", "3000"]
