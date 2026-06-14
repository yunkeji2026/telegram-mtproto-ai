// 无界科技 BOUNDLESS 品牌标识。当前沿用过渡期图形资源（hualing-mark-256.png），
// 全新「界」字 logo 资产在视觉重制阶段替换；此处文案层已统一为无界科技。
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
      aria-label="无界科技 BOUNDLESS"
    >
      <Image
        src="/brand/logos/hualing-mark-256.png"
        alt="无界科技 BOUNDLESS"
        width={72}
        height={72}
        className="h-full w-full object-contain"
        priority
        draggable={false}
      />
    </span>
  );
}
