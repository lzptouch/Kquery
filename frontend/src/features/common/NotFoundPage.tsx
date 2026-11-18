/** 未匹配路由的兜底。没有它，深链 404 表现为一片空白，用户无从判断是地址错了还是服务坏了。 */
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="empty-state" role="alert">
      <p className="empty-title">页面不存在</p>
      <p>请检查地址是否正确，或从左侧导航重新进入。</p>
      <p className="muted">
        <Link to="/">返回对话</Link>
      </p>
    </div>
  );
}
