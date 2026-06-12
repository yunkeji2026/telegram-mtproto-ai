// 华灵科技 HuaLing Tech 品牌标识：云 × 无限 × 心 三合一双环交缠形，
// 蓝→紫→橙活力立体渐变。源图见 public/brand/logos/hualing-mark.png（AI 3D 主形），
// 此处用 256px 优化版用于导航/页脚等小尺寸场景。

export default function BrandMark({
  className = "h-9 w-9",
  rounded = false,
}: {
  className?: string;
  rounded?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center justify-center ${rounded ? "rounded-[22%] bg-[#05060f] p-[6%]" : ""} ${className}`}
      role="img"
      aria-label="华灵科技 HuaLing Tech"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/logos/hualing-mark-256.png"
        alt="华灵科技 HuaLing Tech"
        className="h-full w-full object-contain"
        draggable={false}
      />
    </span>
  );
}
