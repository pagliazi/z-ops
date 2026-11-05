import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:7789/api/:path*',
      },
    ];
  },
  images: {
    unoptimized: true,
  },
  allowedDevOrigins: ['198.18.18.147', 'localhost', 'opscore-ai.example.com'],
};

export default nextConfig;
