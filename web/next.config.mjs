/** @type {import('next').NextConfig} */
// No rewrites() here on purpose. /api/* is handled by app/api/[...path]/route.ts,
// which proxies to CALLHARNESS_API_URL and attaches CALLHARNESS_API_KEY server-side.
// A rewrite cannot add that header, and its destination is baked in at build time;
// see the comment block in that route for the full reasoning.
const nextConfig = {};

export default nextConfig;
