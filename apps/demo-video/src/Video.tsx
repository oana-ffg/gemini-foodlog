import {Audio, Video} from "@remotion/media";
import type {CSSProperties, ReactNode} from "react";
import {
  AbsoluteFill,
  Img,
  Sequence,
  getStaticFiles,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import content from "./content.json";
import ttsManifest from "./generated/tts-manifest.json";
import {fontFamily, palette} from "./theme";

type Scene = (typeof content.scenes)[number];
type NarrationEntry = {
  hash: string;
  file: string;
  durationSeconds: number;
  characters: number;
};
type NarrationEntries = Record<string, NarrationEntry>;

const transitionFrames = 18;
const narrationTailSeconds = 1.4;

export const sceneDurationInFrames = (
  scene: Scene,
  entries: NarrationEntries,
  fps: number,
) => {
  const narrationDuration = entries[scene.id]?.durationSeconds ?? 0;
  return Math.ceil(Math.max(scene.minimumSeconds, narrationDuration + narrationTailSeconds) * fps);
};

export const totalDurationInFrames = (
  scenes: readonly Scene[],
  entries: NarrationEntries,
  fps: number,
) => scenes.reduce((sum, scene) => sum + sceneDurationInFrames(scene, entries, fps), 0);

const Grain = () => (
  <AbsoluteFill
    style={{
      opacity: 0.08,
      backgroundImage:
        "radial-gradient(circle at 18% 20%, #ffffff 0 1px, transparent 1.5px), radial-gradient(circle at 74% 62%, #ffffff 0 1px, transparent 1.5px)",
      backgroundSize: "13px 13px, 17px 17px",
      mixBlendMode: "soft-light",
      pointerEvents: "none",
    }}
  />
);

const SceneFade = ({duration, children}: {duration: number; children: ReactNode}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(
    frame,
    [0, transitionFrames, duration - transitionFrames, duration],
    [0, 1, 1, 0],
    {extrapolateLeft: "clamp", extrapolateRight: "clamp"},
  );
  return <AbsoluteFill style={{opacity}}>{children}</AbsoluteFill>;
};

const Eyebrow = ({children, light = false}: {children: ReactNode; light?: boolean}) => (
  <div
    style={{
      color: light ? palette.amber : palette.coral,
      fontFamily,
      fontSize: 22,
      fontWeight: 800,
      letterSpacing: 4,
      lineHeight: 1.2,
      textTransform: "uppercase",
    }}
  >
    {children}
  </div>
);

const CaptionPill = ({children, dark = false}: {children: ReactNode; dark?: boolean}) => (
  <div
    style={{
      alignSelf: "flex-start",
      border: `1px solid ${dark ? "rgba(255,255,255,.24)" : "rgba(22,63,49,.18)"}`,
      borderRadius: 999,
      color: dark ? palette.cream : palette.forest,
      fontFamily,
      fontSize: 23,
      fontWeight: 650,
      lineHeight: 1.25,
      padding: "14px 22px",
      background: dark ? "rgba(11,35,27,.64)" : "rgba(255,253,247,.84)",
      backdropFilter: "blur(16px)",
    }}
  >
    {children}
  </div>
);

const BrandLockup = ({compact = false}: {compact?: boolean}) => (
  <div style={{display: "flex", alignItems: "center", gap: compact ? 16 : 24}}>
    <Img
      src={staticFile("generated/brand/foodlog-mark.svg")}
      style={{width: compact ? 66 : 126, height: compact ? 66 : 126}}
    />
    <div style={{fontFamily, color: palette.cream, fontWeight: 850, fontSize: compact ? 28 : 58}}>
      Gemini FoodLog
    </div>
  </div>
);

const AppWindow = ({children, style}: {children: ReactNode; style?: CSSProperties}) => (
  <div
    style={{
      background: palette.cream,
      borderRadius: 28,
      boxShadow: "0 36px 90px rgba(13, 35, 27, .28)",
      overflow: "hidden",
      border: "1px solid rgba(22,63,49,.12)",
      ...style,
    }}
  >
    <div
      style={{
        height: 54,
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "0 22px",
        background: "#e9e4da",
        borderBottom: "1px solid rgba(22,63,49,.1)",
      }}
    >
      {[palette.coral, palette.amber, "#6cb483"].map((color) => (
        <div key={color} style={{width: 14, height: 14, borderRadius: 999, background: color}} />
      ))}
      <div
        style={{
          marginLeft: 18,
          flex: 1,
          height: 24,
          borderRadius: 999,
          background: "rgba(255,255,255,.68)",
        }}
      />
    </div>
    {children}
  </div>
);

const CinematicScene = ({scene}: {scene: Scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const reveal = spring({frame, fps, config: {damping: 18, stiffness: 90}});
  const available = new Set(getStaticFiles().map((file) => file.name));
  const hasVideo = scene.video && available.has(scene.video);
  const fallbackImage = scene.fallbackImage;

  return (
    <AbsoluteFill style={{background: palette.forest}}>
      {hasVideo && scene.video ? (
        <Video
          src={staticFile(scene.video)}
          muted
          loop
          style={{width: "100%", height: "100%", objectFit: "cover"}}
        />
      ) : fallbackImage ? (
        <Img
          src={staticFile(fallbackImage)}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            transform: `scale(${1.07 + frame / 9000})`,
            filter: "saturate(.74) contrast(.9) brightness(.58) blur(1px)",
          }}
        />
      ) : null}
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(90deg, rgba(9,31,24,.94) 0%, rgba(9,31,24,.78) 48%, rgba(9,31,24,.22) 100%)",
        }}
      />
      <div
        style={{
          position: "absolute",
          left: 104,
          top: 128,
          width: 1020,
          transform: `translateY(${(1 - reveal) * 48}px)`,
          opacity: reveal,
          display: "flex",
          flexDirection: "column",
          gap: 34,
        }}
      >
        <Eyebrow light>{scene.eyebrow}</Eyebrow>
        <div style={{fontFamily, fontWeight: 860, color: palette.cream, fontSize: 80, lineHeight: 1.03}}>
          {scene.title}
        </div>
        <CaptionPill dark>{scene.caption}</CaptionPill>
      </div>
      {!hasVideo ? (
        <div
          style={{
            position: "absolute",
            right: 56,
            bottom: 44,
            color: "rgba(255,255,255,.7)",
            fontFamily,
            fontSize: 18,
          }}
        >
          Cinematic clip slot · still fallback active
        </div>
      ) : null}
      <Grain />
    </AbsoluteFill>
  );
};

const LogoScene = ({scene}: {scene: Scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const entrance = spring({frame, fps, config: {damping: 12, mass: 0.7, stiffness: 92}});
  const ring = interpolate(frame, [0, 90], [-18, 8], {extrapolateRight: "clamp"});

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 50% 42%, ${palette.forestLight} 0%, ${palette.forest} 52%, #0c2c22 100%)`,
        alignItems: "center",
        justifyContent: "center",
        fontFamily,
      }}
    >
      <div
        style={{
          width: 360,
          height: 360,
          borderRadius: 999,
          border: `2px solid rgba(244,188,85,${0.2 + entrance * 0.35})`,
          transform: `scale(${0.55 + entrance * 0.45}) rotate(${ring}deg)`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxShadow: "0 0 140px rgba(240,90,53,.18)",
        }}
      >
        <Img src={staticFile("generated/brand/foodlog-mark.svg")} style={{width: 280, height: 280}} />
      </div>
      <div style={{height: 48}} />
      <Eyebrow light>{scene.eyebrow}</Eyebrow>
      <div style={{color: palette.cream, fontWeight: 860, fontSize: 76, marginTop: 20}}>{scene.title}</div>
      <div style={{marginTop: 34}}>
        <CaptionPill dark>{scene.caption}</CaptionPill>
      </div>
      <Grain />
    </AbsoluteFill>
  );
};

const ArchitectureScene = ({scene}: {scene: Scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const nodes = [
    ["Cheap camera", "ordinary frames"],
    ["Cloud Run", "authenticated ingest"],
    ["Google ADK", "bounded tools"],
    ["Gemini 3.6", "blind read → context"],
    ["Food timeline", "evidence + revisions"],
  ] as const;

  return (
    <AbsoluteFill style={{background: palette.paper, fontFamily, padding: "80px 92px"}}>
      <Eyebrow>{scene.eyebrow}</Eyebrow>
      <div style={{fontSize: 60, lineHeight: 1.08, fontWeight: 850, color: palette.ink, marginTop: 18, maxWidth: 1500}}>
        {scene.title}
      </div>
      <div style={{display: "flex", alignItems: "center", gap: 14, marginTop: 112}}>
        {nodes.map(([label, detail], index) => {
          const enter = spring({frame: frame - index * 12, fps, config: {damping: 16, stiffness: 110}});
          return (
            <div key={label} style={{display: "flex", alignItems: "center", gap: 14}}>
              <div
                style={{
                  width: 286,
                  height: 230,
                  borderRadius: 28,
                  background: index === 3 ? palette.forest : palette.cream,
                  color: index === 3 ? palette.cream : palette.ink,
                  padding: 30,
                  boxSizing: "border-box",
                  boxShadow: "0 22px 55px rgba(22,63,49,.12)",
                  border: `1px solid ${index === 3 ? palette.forestLight : "rgba(22,63,49,.12)"}`,
                  opacity: enter,
                  transform: `translateY(${(1 - enter) * 34}px)`,
                }}
              >
                <div style={{fontSize: 29, fontWeight: 820, lineHeight: 1.1}}>{label}</div>
                <div style={{fontSize: 21, color: index === 3 ? palette.mist : palette.muted, marginTop: 20, lineHeight: 1.35}}>
                  {detail}
                </div>
                <div
                  style={{
                    width: 42,
                    height: 8,
                    borderRadius: 99,
                    marginTop: 32,
                    background: index === 3 ? palette.coral : palette.amber,
                  }}
                />
              </div>
              {index < nodes.length - 1 ? (
                <div style={{color: palette.coral, fontWeight: 900, fontSize: 36, opacity: enter}}>→</div>
              ) : null}
            </div>
          );
        })}
      </div>
      <div style={{position: "absolute", left: 92, bottom: 70}}>
        <CaptionPill>{scene.caption}</CaptionPill>
      </div>
      <Grain />
    </AbsoluteFill>
  );
};

const ScreenshotScene = ({scene, split = false}: {scene: Scene; split?: boolean}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const drift = interpolate(frame, [0, durationInFrames], [1.015, 1.055]);

  return (
    <AbsoluteFill style={{background: palette.paper, fontFamily, padding: "62px 78px", boxSizing: "border-box"}}>
      <div style={{display: "grid", gridTemplateColumns: "540px 1fr", gap: 52, height: "100%", alignItems: "center"}}>
        <div style={{display: "flex", flexDirection: "column", gap: 28}}>
          <Eyebrow>{scene.eyebrow}</Eyebrow>
          <div style={{fontSize: 65, lineHeight: 1.02, fontWeight: 860, color: palette.ink}}>{scene.title}</div>
          <CaptionPill>{scene.caption}</CaptionPill>
          <div style={{display: "flex", alignItems: "center", gap: 14, color: palette.muted, fontSize: 19, marginTop: 14}}>
            <span style={{width: 9, height: 9, borderRadius: 999, background: "#3d9a68"}} />
            Deployed product · synthetic judge account
          </div>
        </div>
        <AppWindow style={{height: 880}}>
          <div style={{height: 826, display: "flex", overflow: "hidden", background: "#efeae1"}}>
            {scene.image ? (
              <Img
                src={staticFile(scene.image)}
                style={{
                  width: split ? "50%" : "100%",
                  height: "100%",
                  objectFit: "contain",
                  transform: `scale(${drift})`,
                }}
              />
            ) : null}
            {split && scene.secondaryImage ? (
              <Img
                src={staticFile(scene.secondaryImage)}
                style={{width: "50%", height: "100%", objectFit: "contain", borderLeft: "2px solid #d5cec0"}}
              />
            ) : null}
          </div>
        </AppWindow>
      </div>
      <Grain />
    </AbsoluteFill>
  );
};

const ClosingScene = ({scene}: {scene: Scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, stiffness: 72}});
  return (
    <AbsoluteFill style={{background: palette.forest, fontFamily, overflow: "hidden"}}>
      {scene.image ? (
        <Img
          src={staticFile(scene.image)}
          style={{width: "100%", height: "100%", objectFit: "cover", filter: "brightness(.25) saturate(.65) blur(2px)"}}
        />
      ) : null}
      <AbsoluteFill style={{background: "linear-gradient(90deg, rgba(7,27,20,.97), rgba(7,27,20,.79))"}} />
      <div
        style={{
          position: "absolute",
          left: 110,
          top: 130,
          right: 110,
          bottom: 72,
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          opacity: enter,
          transform: `translateY(${(1 - enter) * 40}px)`,
        }}
      >
        <BrandLockup />
        <div>
          <div style={{color: palette.cream, fontSize: 88, lineHeight: 1, fontWeight: 880, maxWidth: 1420}}>{scene.title}</div>
          <div style={{color: palette.mist, fontSize: 31, lineHeight: 1.4, marginTop: 36, maxWidth: 1300}}>{scene.caption}</div>
        </div>
        <div style={{display: "flex", justifyContent: "space-between", alignItems: "flex-end", color: "rgba(255,253,247,.68)", fontSize: 18}}>
          <div>Created for Google's 2026 All Things Agentic Hackathon</div>
          <div style={{textAlign: "right"}}>Narration uses an AI-generated OpenAI voice.</div>
        </div>
      </div>
      <Grain />
    </AbsoluteFill>
  );
};

const renderScene = (scene: Scene) => {
  switch (scene.kind) {
    case "cinematic":
      return <CinematicScene scene={scene} />;
    case "logo":
      return <LogoScene scene={scene} />;
    case "architecture":
      return <ArchitectureScene scene={scene} />;
    case "split-screenshot":
      return <ScreenshotScene scene={scene} split />;
    case "screenshot":
      return <ScreenshotScene scene={scene} />;
    case "closing":
      return <ClosingScene scene={scene} />;
    default:
      return null;
  }
};

export const FoodLogDemo = () => {
  const entries = ttsManifest.entries as NarrationEntries;
  let offset = 0;
  return (
    <AbsoluteFill style={{background: palette.forest}}>
      {content.scenes.map((scene) => {
        const duration = sceneDurationInFrames(scene, entries, content.fps);
        const from = offset;
        offset += duration;
        const audio = entries[scene.id];
        return (
          <Sequence key={scene.id} name={scene.id} from={from} durationInFrames={duration} premountFor={30}>
            <SceneFade duration={duration}>{renderScene(scene)}</SceneFade>
            {audio ? <Audio src={staticFile(audio.file)} volume={0.98} /> : null}
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
