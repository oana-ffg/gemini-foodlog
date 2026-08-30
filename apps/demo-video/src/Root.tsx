import {Composition} from "remotion";

import content from "./content.json";
import ttsManifest from "./generated/tts-manifest.json";
import {FoodLogDemo, totalDurationInFrames} from "./Video";

export const RemotionRoot = () => (
  <Composition
    id="FoodLogDemo"
    component={FoodLogDemo}
    durationInFrames={totalDurationInFrames(content.scenes, ttsManifest.entries, content.fps)}
    fps={content.fps}
    width={content.width}
    height={content.height}
  />
);
