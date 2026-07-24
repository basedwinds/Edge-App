import { Routes, Route } from "react-router-dom";
import { Sidebar } from "./components/layout/Sidebar";
import { Dashboard } from "./pages/Dashboard";
import { Futures } from "./pages/Futures";
import { Recommended } from "./pages/Recommended";
import { Placed } from "./pages/Placed";
import { Calibration } from "./pages/Calibration";
import { Settings } from "./pages/Settings";
import { NewMarkets } from "./pages/NewMarkets";
import { Racing } from "./pages/Racing";
import { Backtests } from "./pages/Backtests";
import { MarketDetail } from "./pages/MarketDetail";
import { Nba } from "./pages/Nba";
import { NbaFutures } from "./pages/NbaFutures";
import { NbaRecommended } from "./pages/NbaRecommended";
import { NbaPlaced } from "./pages/NbaPlaced";
import { NbaCalibration } from "./pages/NbaCalibration";
import { WnbaRecommended } from "./pages/WnbaRecommended";
import { WnbaPlaced } from "./pages/WnbaPlaced";
import { WnbaCalibration } from "./pages/WnbaCalibration";
import { Divergences } from "./pages/Divergences";
import { ClvBuckets } from "./pages/ClvBuckets";
import { Health } from "./pages/Health";
import { Combined } from "./pages/Combined";
import { Tracker } from "./pages/Tracker";
import { Mlb } from "./pages/Mlb";
import { MlbFutures } from "./pages/MlbFutures";
import { MlbRecommended } from "./pages/MlbRecommended";
import { MlbPlaced } from "./pages/MlbPlaced";
import { MlbCalibration } from "./pages/MlbCalibration";
import { Mma } from "./pages/Mma";
import { MmaRecommended } from "./pages/MmaRecommended";
import { MmaPlaced } from "./pages/MmaPlaced";
import { MmaCalibration } from "./pages/MmaCalibration";
import { Tennis } from "./pages/Tennis";
import { TennisFutures } from "./pages/TennisFutures";
import { TennisRecommended } from "./pages/TennisRecommended";
import { TennisPlaced } from "./pages/TennisPlaced";
import { TennisCalibration } from "./pages/TennisCalibration";
import { Soccer } from "./pages/Soccer";
import { SoccerFutures } from "./pages/SoccerFutures";
import { SoccerRecommended } from "./pages/SoccerRecommended";
import { SoccerPlaced } from "./pages/SoccerPlaced";
import { SoccerCalibration } from "./pages/SoccerCalibration";
import { Valorant } from "./pages/Valorant";
import { ValorantFutures } from "./pages/ValorantFutures";
import { ValorantRecommended } from "./pages/ValorantRecommended";
import { ValorantPlaced } from "./pages/ValorantPlaced";
import { ValorantCalibration } from "./pages/ValorantCalibration";
import { Cs2 } from "./pages/Cs2";
import { Cs2Futures } from "./pages/Cs2Futures";
import { Cs2Recommended } from "./pages/Cs2Recommended";
import { Cs2Placed } from "./pages/Cs2Placed";
import { Cs2Calibration } from "./pages/Cs2Calibration";
import { Lol } from "./pages/Lol";
import { LolFutures } from "./pages/LolFutures";
import { LolRecommended } from "./pages/LolRecommended";
import { LolPlaced } from "./pages/LolPlaced";
import { LolCalibration } from "./pages/LolCalibration";

function App() {
  return (
    <div className="flex h-full">
      <Sidebar />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/futures" element={<Futures />} />
        <Route path="/recommended" element={<Recommended />} />
        <Route path="/placed" element={<Placed />} />
        <Route path="/calibration" element={<Calibration />} />
        <Route path="/markets/:key" element={<MarketDetail />} />
        <Route path="/backtests" element={<Backtests />} />
        <Route path="/nba" element={<Nba />} />
        <Route path="/nba/futures" element={<NbaFutures />} />
        <Route path="/nba/recommended" element={<NbaRecommended />} />
        <Route path="/nba/placed" element={<NbaPlaced />} />
        <Route path="/nba/calibration" element={<NbaCalibration />} />
        <Route path="/wnba/recommended" element={<WnbaRecommended />} />
        <Route path="/wnba/placed" element={<WnbaPlaced />} />
        <Route path="/wnba/calibration" element={<WnbaCalibration />} />
        <Route path="/all" element={<Combined />} />
        <Route path="/tracker" element={<Tracker />} />
        <Route path="/divergences" element={<Divergences />} />
        <Route path="/clv-buckets" element={<ClvBuckets />} />
        <Route path="/health" element={<Health />} />
        <Route path="/mlb" element={<Mlb />} />
        <Route path="/mlb/futures" element={<MlbFutures />} />
        <Route path="/mlb/recommended" element={<MlbRecommended />} />
        <Route path="/mlb/placed" element={<MlbPlaced />} />
        <Route path="/mlb/calibration" element={<MlbCalibration />} />
        <Route path="/mma" element={<Mma />} />
        <Route path="/mma/recommended" element={<MmaRecommended />} />
        <Route path="/mma/placed" element={<MmaPlaced />} />
        <Route path="/mma/calibration" element={<MmaCalibration />} />
        <Route path="/tennis" element={<Tennis />} />
        <Route path="/tennis/futures" element={<TennisFutures />} />
        <Route path="/tennis/recommended" element={<TennisRecommended />} />
        <Route path="/tennis/placed" element={<TennisPlaced />} />
        <Route path="/tennis/calibration" element={<TennisCalibration />} />
        <Route path="/soccer" element={<Soccer />} />
        <Route path="/soccer/futures" element={<SoccerFutures />} />
        <Route path="/soccer/recommended" element={<SoccerRecommended />} />
        <Route path="/soccer/placed" element={<SoccerPlaced />} />
        <Route path="/soccer/calibration" element={<SoccerCalibration />} />
        <Route path="/valorant" element={<Valorant />} />
        <Route path="/valorant/futures" element={<ValorantFutures />} />
        <Route path="/valorant/recommended" element={<ValorantRecommended />} />
        <Route path="/valorant/placed" element={<ValorantPlaced />} />
        <Route path="/valorant/calibration" element={<ValorantCalibration />} />
        <Route path="/cs2" element={<Cs2 />} />
        <Route path="/cs2/futures" element={<Cs2Futures />} />
        <Route path="/cs2/recommended" element={<Cs2Recommended />} />
        <Route path="/cs2/placed" element={<Cs2Placed />} />
        <Route path="/cs2/calibration" element={<Cs2Calibration />} />
        <Route path="/lol" element={<Lol />} />
        <Route path="/lol/futures" element={<LolFutures />} />
        <Route path="/lol/recommended" element={<LolRecommended />} />
        <Route path="/lol/placed" element={<LolPlaced />} />
        <Route path="/lol/calibration" element={<LolCalibration />} />
        <Route path="/new-markets" element={<NewMarkets />} />
        <Route path="/racing" element={<Racing />} />
        <Route path="/racing/:series" element={<Racing />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </div>
  );
}

export default App;
