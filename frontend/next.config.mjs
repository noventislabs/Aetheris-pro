/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The terminal renders numbers, not media; no image optimisation pipeline is
  // needed and skipping it keeps the dev server's memory footprint down on the
  // 8 GB target machine.
  images: { unoptimized: true },
};

export default nextConfig;
