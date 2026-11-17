import { LayoutComponent } from '@gitroom/frontend/components/new-layout/layout.component';

export default async function Layout({
  children,
}: {
  children: React.ReactNode;
}) {
  const studioUrl = process.env.VIDEO_STUDIO_URL?.trim();
  const url =
    studioUrl && URL.canParse(studioUrl) ? new URL(studioUrl) : undefined;
  const videoStudioUrl =
    url &&
    (url.protocol === 'http:' || url.protocol === 'https:') &&
    !url.username &&
    !url.password
      ? url.href
      : undefined;
  return (
    <LayoutComponent videoStudioUrl={videoStudioUrl}>
      {children}
    </LayoutComponent>
  );
}
