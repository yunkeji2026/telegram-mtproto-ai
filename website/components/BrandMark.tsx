// 华灵科技 HuaLing Tech 品牌标识：云 × 无限 × 心 三合一双环交缠形，蓝→紫→橙活力立体渐变。
// 源图见 public/brand/logos/hualing-mark.png（AI 3D 主形，已抠成真透明）。
// 这里用 next/image 渲染 256px 版：生产期自动出 AVIF/WebP、按需缩放，导航/页脚轻量。

import Image from "next/image";

export default function BrandMark({
  className = "h-9 w-9",
  rounded = false,
}: {
  className?: string;
  rounded?: boolean;
}) {
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center ${rounded ? "rounded-[22%] bg-[#05060f] p-[6%]" : ""} ${className}`}
      role="img"
      aria-label="华灵科技 HuaLing Tech"
    >
      <Image
        src="/brand/logos/hualing-mark-256.png"
        alt="华灵科技 HuaLing Tech"
        width={72}
        height={72}
        className="h-full w-full object-contain"
        priority
        draggable={false}
      />
    </span>
  );
}
