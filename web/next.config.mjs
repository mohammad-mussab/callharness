/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    const api = process.env.CALLHARNESS_API_URL || "http://127.0.0.1:8010";
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
};

export default nextConfig;
