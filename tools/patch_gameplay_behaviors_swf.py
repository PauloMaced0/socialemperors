#!/usr/bin/env python3
"""Rebuild the small ActionScript fixes used by the gameplay-state audit.

This patch intentionally edits source-level methods instead of replacing the
whole game SWF. It preserves every prior click/attack/HUD patch and changes:

* Fast Collect collects a ready producer when the cursor enters its sprite.
* stock same-tile gold/stone regeneration objects stay disabled; the server
  owns their three-hour timers and creates capped replacements at random wild
  positions, while browser reloads cannot rerun initial map population;
* partial building and home-unit health loads from/saves to ``attrs.hp``;
* Training Stables hides the generic ``0% / 0 gold`` producer display because
  its horse-plus-infantry conversion is immediate rather than timer-based;
* Fire Havoc centers its target area on the clicked tile;
* social-building friend slots create an acceptance request instead of filling
  themselves immediately;
* reload no longer inserts hidden social-building workers, and unfinished
  staffed buildings cannot expose Market/upgrade/training actions;
* the stock Harbor reload hook no longer grants free staff or announces
  "Dock operative"; Ship Land remains locked until the roster is complete;
* an unfinished Cathedral shows its building description instead of Monk
  price/training controls;
* completed staff carry to matching job titles on upgraded tiers, while only
  genuinely new roles remain vacant;
* setup popups leave their building selected, so closing the staffing/time
  chooser still exposes the normal move, store and sell controls;
* the quest-result "saving results" overlay is dismissed after the command is
  queued, so Skip does not leave an uncloseable window.
* claiming the daily bonus keeps a non-zero local timestamp during the first
  server-clock synchronization, so the recurring-event poll cannot reopen the
  already claimed prize.
* the Mini Fireball projectile accepts the compatible class name exported by
  its bundled effect SWF instead of throwing during asset loading.

Requires JPEXS FFDec. Example:

    python tools/patch_gameplay_behaviors_swf.py \
        --ffdec /path/to/ffdec.jar
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWF = ROOT / "assets" / "flash" / "SocialEmpires0926bsec.swf"
CLASSES = ",".join((
    "core.Base",
    "core.MapInitializer",
    "core.isoengine.IsoElement",
    "core.isoengine.IsoInteractiveElement",
    "core.isoengine.IsoFightingElement",
    "core.isoengine.IsoBuilding",
    "core.isoengine.IsoEngine",
    "GUI.RecuadroInfo",
    "core.magic.SpellFireBall",
    "managers.Projectiles",
    "popups.PopupCollect",
    "popups.PopupSocialBuilding",
    "popups.PopupQuestsManager",
    "popups.PopupQuestFinished",
    "popups.PopupNewDaily",
))
SCRIPT_FILES = (
    Path("core/Base.as"),
    Path("core/MapInitializer.as"),
    Path("core/isoengine/IsoElement.as"),
    Path("core/isoengine/IsoInteractiveElement.as"),
    Path("core/isoengine/IsoFightingElement.as"),
    Path("core/isoengine/IsoBuilding.as"),
    Path("core/isoengine/IsoEngine.as"),
    Path("GUI/RecuadroInfo.as"),
    Path("core/magic/SpellFireBall.as"),
    Path("managers/Projectiles.as"),
    Path("popups/PopupCollect.as"),
    Path("popups/PopupSocialBuilding.as"),
    Path("popups/PopupQuestsManager.as"),
    Path("popups/PopupQuestFinished.as"),
    Path("popups/PopupNewDaily.as"),
)


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path.name}: expected one source signature, found {count}"
        )
    path.write_text(text.replace(old, new, 1))


def patch_sources(scripts: Path) -> None:
    base = scripts / "core" / "Base.as"
    iso_element = scripts / "core" / "isoengine" / "IsoElement.as"
    interactive = scripts / "core" / "isoengine" / "IsoInteractiveElement.as"
    fighting = scripts / "core" / "isoengine" / "IsoFightingElement.as"
    building = scripts / "core" / "isoengine" / "IsoBuilding.as"
    iso_engine = scripts / "core" / "isoengine" / "IsoEngine.as"
    recuadro = scripts / "GUI" / "RecuadroInfo.as"
    initializer = scripts / "core" / "MapInitializer.as"
    fire = scripts / "core" / "magic" / "SpellFireBall.as"
    projectiles = scripts / "managers" / "Projectiles.as"
    popup_collect = scripts / "popups" / "PopupCollect.as"
    social = scripts / "popups" / "PopupSocialBuilding.as"
    quests_manager = scripts / "popups" / "PopupQuestsManager.as"
    quest_finished = scripts / "popups" / "PopupQuestFinished.as"
    popup_daily = scripts / "popups" / "PopupNewDaily.as"

    # countAllBuildings() used to silently insert the first worker when an
    # empty social-building staff array was loaded. That command has a hidden
    # fifth argument and is not the result of either cash purchase or accepted
    # friend help.
    replace_once(
        base,
        """               if(_loc8_ != null && Number(_loc5_.worker_cost) > 0)
               {
                  if(_loc8_.length == 0 && _loc7_ >= 3 || _loc8_.length == 1 && _loc7_ >= 7)
                  {
                     Base.Commands.addCommand({
                        "cmd":Constants.CMD_BUY_SI_HELP,
                        "args":[_loc6_.tx,_loc6_.ty,Base.Main.townID,_loc2_,1]
                     });
                     _loc6_.loaded.attrs.si.push(0);
                     IsoBuilding(_loc6_.mc).glowAnimation();
                  }
               }""",
        """               if(_loc8_ != null && Number(_loc5_.worker_cost) > 0)
               {
                  // Staff state is server-authoritative. Empty means every
                  // role is still vacant; reload must not hire one silently.
               }""",
    )

    replace_once(
        iso_element,
        """            IsoFightingElement(this).iHealth = this.buildingReference.building.life;
            IsoFightingElement(this).uiAttack = this.buildingReference.building.attack;""",
        """            IsoFightingElement(this).iHealth = this.buildingReference.building.life;
            if(this.buildingReference.loaded != null && this.buildingReference.loaded.attrs != null && this.buildingReference.loaded.attrs.hp != null)
            {
               IsoFightingElement(this).iHealth = Math.max(0,Math.min(this.buildingReference.building.life,int(this.buildingReference.loaded.attrs.hp)));
            }
            IsoFightingElement(this).uiAttack = this.buildingReference.building.attack;""",
    )

    replace_once(
        interactive,
        """         var _loc4_:Boolean = false;
         if(param1 != null && this is IsoBuilding && this.iconCollect == null && this.iconsHurryUp.length == 0)""",
        """         if(Base.Main.spdclct && this is IsoBuilding && !Base.Main.IsBeingBuilt(this) && this.canCollect() && (this.PlayerID == Constants.PLAYER_SELF || this.PlayerID == Constants.PLAYER_NEUTRAL))
         {
            this.startCollection();
            return;
         }
         var _loc4_:Boolean = false;
         if(param1 != null && this is IsoBuilding && this.iconCollect == null && this.iconsHurryUp.length == 0)""",
    )

    replace_once(
        interactive,
        """               {
                  Base.PopUp.openPopupSocial(IsoBuilding(this));
                  return;
               }""",
        """               {
                  Base.Main.deseleccionarElementos();
                  Base.Main.seleccionarElemento(this);
                  Base.PopUp.openPopupSocial(IsoBuilding(this));
                  return;
               }""",
    )
    replace_once(
        interactive,
        """               {
                  Base.PopUp.openPopupCollect(this);
                  return;
               }""",
        """               {
                  Base.Main.deseleccionarElementos();
                  Base.Main.seleccionarElemento(this);
                  Base.PopUp.openPopupCollect(this);
                  return;
               }""",
    )

    replace_once(
        fighting,
        """            if(this is IsoUnit && Base.Main.bBM)
            {""",
        """            if((this is IsoBuilding || this is IsoUnit) && this.PlayerID == Constants.PLAYER_SELF && Base.Main.gameMode == Constants.GAME_MODE_NORMAL)
            {
               Base.Commands.addCommand({
                  "cmd":"set_item_health",
                  "args":[this.buildingReference.tx,this.buildingReference.ty,Base.Main.townID,this.buildingReference.building.id,this.iHealth]
               });
            }
            if(this is IsoUnit && Base.Main.bBM)
            {""",
    )

    replace_once(
        fighting,
        """            if(param2 != null && param2.delegate != null)
            {
               param2.delegate.onDealedHealth(param1);
            }
            this.updateVida();""",
        """            if(param2 != null && param2.delegate != null)
            {
               param2.delegate.onDealedHealth(param1);
            }
            if((this is IsoBuilding || this is IsoUnit) && this.PlayerID == Constants.PLAYER_SELF && Base.Main.gameMode == Constants.GAME_MODE_NORMAL)
            {
               Base.Commands.addCommand({
                  "cmd":"set_item_health",
                  "args":[this.buildingReference.tx,this.buildingReference.ty,Base.Main.townID,this.buildingReference.building.id,this.iHealth]
               });
            }
            this.updateVida();""",
    )

    replace_once(
        building,
        """         this.iHealth = Math.min(this.iHealth + Config.INCREMENT_HEALTH_REPAIR,this.buildingReference.building.life);
         Base.Main.ps.addParticle""",
        """         this.iHealth = Math.min(this.iHealth + Config.INCREMENT_HEALTH_REPAIR,this.buildingReference.building.life);
         if(this.PlayerID == Constants.PLAYER_SELF && Base.Main.gameMode == Constants.GAME_MODE_NORMAL)
         {
            Base.Commands.addCommand({
               "cmd":"set_item_health",
               "args":[this.buildingReference.tx,this.buildingReference.ty,Base.Main.townID,this.buildingReference.building.id,this.iHealth]
            });
         }
         Base.Main.ps.addParticle""",
    )

    replace_once(
        recuadro,
        "   public class RecuadroInfo extends MovieClip\n   {\n",
        """   public class RecuadroInfo extends MovieClip
   {
      
      private static const TRAINING_STABLE_UI_FIX:String = "socialemperors-training-stable-ui-v1";
      
      private static const STAFFED_BUILDING_ACTION_FIX:String = "socialemperors-staffed-building-actions-v1";
      
      private static const CATHEDRAL_UNSTAFFED_DESCRIPTION_FIX:String = "socialemperors-cathedral-unstaffed-description-v2";
""",
    )
    replace_once(
        recuadro,
        """                        this.extendedPortrait.btMarket.addEventListener(MouseEvent.CLICK,this.openMarket);
                        this.loadImage(elementInfo.img_name);""",
        """                        if(Base.Main.IsBeingBuilt(this.eElement))
                        {
                           this.extendedPortrait.btMarket.visible = false;
                        }
                        else
                        {
                           this.extendedPortrait.btMarket.addEventListener(MouseEvent.CLICK,this.openMarket);
                        }
                        this.loadImage(elementInfo.img_name);""",
    )
    replace_once(
        recuadro,
        """         var _loc5_:int = 0;
         var _loc6_:int = 0;
         _loc4_ = StaticDataLibrary.api.getItem(IsoBuilding(param1).buildingReference.building.trains);""",
        """         var _loc5_:int = 0;
         var _loc6_:int = 0;
         if(Base.Main.IsBeingBuilt(param1))
         {
            this.extendedPortrait.mcImageTrainable.visible = false;
            this.extendedPortrait.menu.visible = false;
            this.extendedPortrait.txNombreUnidad.visible = false;
            this.extendedPortrait.cost.visible = false;
            this.extendedPortrait.costText.visible = false;
            this.extendedPortrait.iconResources.visible = false;
            if(this.extendedPortrait.costFood != null)
            {
               this.extendedPortrait.costFood.visible = false;
            }
            if(this.extendedPortrait.iconResourcesFood != null)
            {
               this.extendedPortrait.iconResourcesFood.visible = false;
            }
            if(this.extendedPortrait.mcBuyWithCash != null)
            {
               this.extendedPortrait.mcBuyWithCash.visible = false;
            }
            return;
         }
         _loc4_ = StaticDataLibrary.api.getItem(IsoBuilding(param1).buildingReference.building.trains);""",
    )
    replace_once(
        recuadro,
        """         if(this.eElement != null && this.eElement is IsoBuilding)
         {
            IsoBuilding(this.eElement).UnitTrainingStart();
         }""",
        """         if(this.eElement != null && this.eElement is IsoBuilding && !Base.Main.IsBeingBuilt(this.eElement))
         {
            IsoBuilding(this.eElement).UnitTrainingStart();
         }""",
    )

    replace_once(
        quests_manager,
        "   public class PopupQuestsManager extends PopupQuestsManagerMC\n   {\n",
        """   public class PopupQuestsManager extends PopupQuestsManagerMC
   {
      
      private static const HARBOUR_STAFFING_GATE:String = "socialemperors-harbour-staffing-gate-v1";
""",
    )
    replace_once(
        quests_manager,
        """      public static function get hasHarbour() : Boolean
      {
         return true;
      }""",
        """      public static function get hasHarbour() : Boolean
      {
         return !(Base.Iso.eDock == null || Base.Main.IsBeingBuilt(Base.Iso.eDock));
      }""",
    )
    replace_once(
        quests_manager,
        """         if(!hasHarbour)
         {
            if(Base.Gui.expolorationWorld != null)
            {
               Base.Gui.expolorationWorld.closeExplorationsManager();
            }
            Base.PopUp.openPopupQuestsManager(PopupQuestsManager.SCROLL_SHIP);
            return true;
         }""",
        """         if(!hasHarbour)
         {
            Base.PopUp.alert(Language.getLiteral(Language.AVISO_BARCO_SIN_MUELLE));
            return false;
         }""",
    )
    replace_once(
        recuadro,
        """         if(this.eElement != null && this.eElement is IsoBuilding)
         {
            IsoBuilding(this.eElement).UnitTrainingStart(true);
         }""",
        """         if(this.eElement != null && this.eElement is IsoBuilding && !Base.Main.IsBeingBuilt(this.eElement))
         {
            IsoBuilding(this.eElement).UnitTrainingStart(true);
         }""",
    )
    replace_once(
        recuadro,
        """               if(_loc1_ != null)
               {
                  this.extendedPortrait.btSell.visible = true;""",
        """               if(_loc1_ != null && !Base.Main.IsBeingBuilt(this.eElement))
               {
                  this.extendedPortrait.btSell.visible = true;""",
    )
    replace_once(
        building,
        """         if(parent == null)
         {
            return false;
         }""",
        """         if(parent == null || Base.Main.IsBeingBuilt(this))
         {
            return false;
         }""",
    )
    replace_once(
        building,
        """         var _loc6_:int = Base.Player.iPopulationCurrent;
         var _loc7_:int = Base.Player.iPopulationMax;
         _loc5_ = StaticDataLibrary.api.getItem(this.buildingReference.building.trains);""",
        """         var _loc6_:int = Base.Player.iPopulationCurrent;
         var _loc7_:int = Base.Player.iPopulationMax;
         if(Base.Main.IsBeingBuilt(this))
         {
            return false;
         }
         _loc5_ = StaticDataLibrary.api.getItem(this.buildingReference.building.trains);""",
    )
    replace_once(
        building,
        """         var _loc4_:Object = null;
         if(this.buildingReference.building.id == Constants.ID_BUILDING_UNIT_WAREHOUSE)""",
        """         var _loc4_:Object = null;
         if(Base.Main.IsBeingBuilt(this))
         {
            return false;
         }
         if(this.buildingReference.building.id == Constants.ID_BUILDING_UNIT_WAREHOUSE)""",
    )
    replace_once(
        building,
        """         var _loc5_:Object = null;
         this.bTrainingUnit = false;
         if(this.buildingReference != null)""",
        """         var _loc5_:Object = null;
         this.bTrainingUnit = false;
         if(Base.Main.IsBeingBuilt(this))
         {
            return;
         }
         if(this.buildingReference != null)""",
    )
    replace_once(
        building,
        """"args":[_loc1_.buildingReference.building.id,_loc2_ % Config.EI_MAP_WIDTH,int(_loc2_ / Config.EI_MAP_WIDTH),_loc1_.buildingReference.frame,Base.Main.townID]""",
        """"args":[_loc1_.buildingReference.building.id,_loc2_ % Config.EI_MAP_WIDTH,int(_loc2_ / Config.EI_MAP_WIDTH),_loc1_.buildingReference.frame,Base.Main.townID,this.buildingReference.tx,this.buildingReference.ty,this.buildingReference.building.id]""",
    )
    replace_once(
        building,
        """"args":[_loc1_.buildingReference.building.id,_loc2_ % Config.EI_MAP_WIDTH,int(_loc2_ / Config.EI_MAP_WIDTH),_loc1_.buildingReference.frame,Base.Main.townID,0,_loc4_,_loc1_.buildingReference.building.type]""",
        """"args":[_loc1_.buildingReference.building.id,_loc2_ % Config.EI_MAP_WIDTH,int(_loc2_ / Config.EI_MAP_WIDTH),_loc1_.buildingReference.frame,Base.Main.townID,0,_loc4_,_loc1_.buildingReference.building.type,this.buildingReference.tx,this.buildingReference.ty,this.buildingReference.building.id]""",
    )
    replace_once(
        building,
        """      public function upgrade() : Boolean
      {""",
        """      private function staffingUpgradeContext() : Object
      {
         var _loc1_:Array = null;
         var _loc2_:Array = null;
         var _loc3_:Object = null;
         var _loc4_:int = 0;
         if(this.buildingReference != null && this.buildingReference.loaded != null && this.buildingReference.loaded.attrs != null)
         {
            if(this.buildingReference.loaded.attrs.staffRoles is Array && this.buildingReference.loaded.attrs.staffRoster is Array)
            {
               _loc1_ = Array(this.buildingReference.loaded.attrs.staffRoles).concat();
               _loc2_ = Array(this.buildingReference.loaded.attrs.staffRoster).concat();
            }
         }
         if(_loc1_ == null || _loc2_ == null)
         {
            _loc3_ = Base.Items.socialItemsMap[this.iID];
            if(_loc3_ != null && !Base.Main.IsBeingBuilt(this))
            {
               _loc1_ = String(_loc3_.workers).split(",");
               _loc2_ = [];
               _loc4_ = 0;
               while(_loc4_ < _loc1_.length)
               {
                  _loc2_.push(0);
                  _loc4_++;
               }
            }
         }
         if(_loc1_ == null || _loc2_ == null)
         {
            return null;
         }
         return {
            "roles":_loc1_,
            "roster":_loc2_
         };
      }
      
      private function applyStaffingUpgradeContext(param1:IsoBuilding, param2:Object) : void
      {
         var _loc3_:Object = null;
         var _loc4_:Array = null;
         var _loc5_:Array = null;
         var _loc6_:Array = null;
         var _loc7_:Array = null;
         var _loc8_:Array = null;
         var _loc9_:int = 0;
         var _loc10_:Object = null;
         if(param1 == null || param1.buildingReference == null || param2 == null)
         {
            return;
         }
         if(param1.buildingReference.loaded == null)
         {
            _loc10_ = new Object();
            _loc10_.attrs = new Object();
            param1.buildingReference.loaded = new DynamicData(_loc10_);
         }
         else if(param1.buildingReference.loaded.attrs == null)
         {
            param1.buildingReference.loaded.attrs = new Object();
         }
         _loc4_ = Array(param2.roles).concat();
         _loc5_ = Array(param2.roster).concat();
         _loc3_ = Base.Items.socialItemsMap[param1.iID];
         if(_loc3_ == null)
         {
            param1.buildingReference.loaded.attrs.staffRoles = _loc4_;
            param1.buildingReference.loaded.attrs.staffRoster = _loc5_;
            return;
         }
         _loc6_ = String(_loc3_.workers).split(",");
         _loc7_ = [];
         _loc8_ = [];
         _loc9_ = 0;
         while(_loc9_ < _loc6_.length && _loc9_ < _loc4_.length && _loc9_ < _loc5_.length)
         {
            if(String(_loc6_[_loc9_]) != String(_loc4_[_loc9_]))
            {
               break;
            }
            _loc7_.push(_loc6_[_loc9_]);
            _loc8_.push(_loc5_[_loc9_]);
            _loc9_++;
         }
         param1.buildingReference.loaded.attrs.staffRoles = _loc7_;
         param1.buildingReference.loaded.attrs.staffRoster = _loc8_;
         param1.buildingReference.loaded.attrs.si = _loc9_ >= _loc6_.length ? null : _loc8_.concat();
      }
      
      public function upgrade() : Boolean
      {""",
    )
    replace_once(
        building,
        """         var _loc11_:String = null;
         var _loc12_:int = 0;
         var _loc3_:Boolean = false;""",
        """         var _loc11_:String = null;
         var _loc12_:int = 0;
         var _loc13_:Object = null;
         var _loc3_:Boolean = false;""",
    )
    replace_once(
        building,
        """         if(_loc3_)
         {
            _loc11_ = this.buildingReference.building.cost_type;""",
        """         if(_loc3_)
         {
            _loc13_ = this.staffingUpgradeContext();
            _loc11_ = this.buildingReference.building.cost_type;""",
    )
    replace_once(
        building,
        """            _loc4_ = IsoBuilding(Base.Main.addElement(_loc10_,_loc2_,true,true,Constants.PLAYER_SELF,true));
            Base.Commands.addCommand({""",
        """            _loc4_ = IsoBuilding(Base.Main.addElement(_loc10_,_loc2_,true,true,Constants.PLAYER_SELF,true));
            this.applyStaffingUpgradeContext(_loc4_,_loc13_);
            Base.Commands.addCommand({""",
    )
    replace_once(
        recuadro,
        """                     case Constants.SUBCATFUNC_BUILDING_CHURCH:
                        this.extendedPortrait = new EP_BARRACKS_MC();""",
        """                     case Constants.SUBCATFUNC_BUILDING_CHURCH:
                        if(elementInfo.id == Constants.ID_BUILDING_CATHEDRAL && Base.Main.IsBeingBuilt(this.eElement))
                        {
                           this.extendedPortrait = new EP_TEXT_MC();
                           this.ri.addChild(this.extendedPortrait);
                           this.initBasicButtons();
                           this.actualizarBarraVida();
                           TextFieldUtil.setHTML(this.extendedPortrait.txNombre,_eElement.sName);
                           TextFieldUtil.setHTML(this.extendedPortrait.mcAttack.txAttack,elementInfo.attack);
                           TextFieldUtil.setHTML(this.extendedPortrait.mcDefense.txDefense,elementInfo.attack_interval);
                           this.loadImage(elementInfo.img_name);
                           TextFieldUtil.setHTML(this.extendedPortrait.tip,Language.getLiteral(Language.STORE_EDIFICIO_CATEDRAL));
                           break;
                        }
                        this.extendedPortrait = new EP_BARRACKS_MC();""",
    )
    replace_once(
        iso_engine,
        "   public class IsoEngine extends EventDispatcher\n   {\n",
        """   public class IsoEngine extends EventDispatcher
   {
      
      private static const HARBOUR_RELOAD_STAFF_FIX:String = "socialemperors-harbour-reload-staff-v1";
""",
    )
    replace_once(
        iso_engine,
        """            if(this.eDock != null && Base.Main.IsBeingBuilt(this.eDock))
            {
               Base.PopUp.openAlerta(Language.getLiteral(Language.AVISO_MUELLE_TITULO),Language.getLiteral(Language.AVISO_MUELLE_DESCRIPCION),"alert_ship2.jpg");
               _loc3_ = 0;
               while(_loc3_ < 20)
               {
                  Base.Commands.addCommand({
                     "cmd":Constants.CMD_BUY_SI_HELP,
                     "args":[this.eDock.buildingReference.tx,this.eDock.buildingReference.ty,Base.Main.townID,this.eDock.iID,1]
                  });
                  _loc3_++;
               }
               Base.Commands.addCommand({
                  "cmd":Constants.CMD_FINISH_SI,
                  "args":[this.eDock.buildingReference.tx,this.eDock.buildingReference.ty,Base.Main.townID,this.eDock.iID]
               });
               if(this.eDock.buildingReference.loaded == null)
               {
                  this.eDock.buildingReference.loaded = new DynamicData(new Object());
               }
               if(this.eDock.buildingReference.loaded.attrs == null)
               {
                  this.eDock.buildingReference.loaded.attrs = new Object();
               }
               this.eDock.buildingReference.loaded.attrs.si = null;
            }""",
        """            // Harbor staffing is player-driven. Reloading must not
            // buy roles, finish the social building, or show Dock operative.
""",
    )
    replace_once(
        recuadro,
        """                           case Constants.ID_BUILDING_STABLE_TRAINING:
                              TextFieldUtil.setHTML(this.extendedPortrait.mcContained.txTip,Language.getLiteral(Language.INFO_CORRAL_CABALLOS));
                              break;""",
        """                           case Constants.ID_BUILDING_STABLE_TRAINING:
                              this.extendedPortrait.iconResources.visible = false;
                              this.extendedPortrait.earns.visible = false;
                              this.extendedPortrait.barraTiempo.visible = false;
                              TextFieldUtil.setHTML(this.extendedPortrait.mcContained.txTip,Language.getLiteral(Language.INFO_CORRAL_CABALLOS));
	                              break;""",
    )
    replace_once(
        recuadro,
        """      private function onFly(param1:Event) : void
      {
         PopupQuestsManager.loadWorldZeppelin(null);
      }""",
        """      private function onFly(param1:Event) : void
      {
         if(!Base.Main.IsBeingBuilt(this.eElement))
         {
            PopupQuestsManager.loadWorldZeppelin(null);
         }
      }""",
    )

    # Keep the fork's arrayAnimals[128] wrappers around only the natural
    # population blocks.  The server removes this marker for the first map
    # population and sets it for established towns; animal spawning remains
    # outside the wrapper and keeps its independent daily allowance.
    text = initializer.read_text()
    guarded_region = text[
        text.index("public static function spawnInitResources"):
        text.index("private static function spawnAnimals")
    ]
    if guarded_region.count("arrayAnimals[128]") != 2:
        raise RuntimeError(
            "MapInitializer natural-resource reload guards missing or duplicated"
        )
    initializer.write_text(text)

    replace_once(fire, "this.iTx = param1 - this.area / 2;", "this.iTx = param1;")
    replace_once(fire, "this.iTy = param2 - this.area / 2;", "this.iTy = param2;")

    # FFDec cannot re-import this subclass's inherited protected accesses
    # without changing their ActionScript namespace. Avoid touching the base
    # class (which would break every other spell) and provide the same rank
    # text through an override that only uses local constants.
    fire_text = fire.read_text()
    fire_text = "\n".join(
        line for line in fire_text.splitlines()
        if "this.arStarDescriptions[" not in line
    ) + "\n"
    fire_text = fire_text.replace(
        """      override public function castSpell(param1:int, param2:int) : void
""",
        """      override public function get rankDescriptions() : Array
      {
         return [
            Language.getLiteral(Language.SPELL_FIREBALL_DESCRIPCION,[5,5,this.DAMAGE[0],this.DAMAGE[0] / 10 * 2]),
            Language.getLiteral(Language.SPELL_FIREBALL_DESCRIPCION,[7,7,this.DAMAGE[1],this.DAMAGE[1] / 10 * 4]),
            Language.getLiteral(Language.SPELL_FIREBALL_DESCRIPCION,[9,9,this.DAMAGE[2],this.DAMAGE[2] / 10 * 6])
         ];
      }
      
      override public function castSpell(param1:int, param2:int) : void
""",
        1,
    )
    fire.write_text(fire_text)

    # IsoElement instances are constructed before Base.initializeMap assigns
    # arIdsCollect. The stock null dereference can abort the progressive map
    # load, leaving the village empty in Ruffle.
    replace_once(
        popup_collect,
        """         if(param1.buildingReference != null)
         {
            _loc2_ = Base.Main.arIdsCollect.indexOf(String(param1.buildingReference.building.id)) >= 0;
         }""",
        """         if(param1.buildingReference != null && Base.Main.arIdsCollect != null)
         {
            _loc2_ = Base.Main.arIdsCollect.indexOf(String(param1.buildingReference.building.id)) >= 0;
         }""",
    )

    old_social_start = social.read_text().index(
        "      public function onFindFriend(param1:Event = null) : void"
    )
    old_social_end = social.read_text().index(
        "      public function onOpenBuilding", old_social_start
    )
    social_text = social.read_text()
    social_method = """      public function onFindFriend(param1:Event = null) : void
      {
         var _loc2_:String = null;
         if(this.si != null && this.si.length < this.arWorkerNames.length)
         {
            _loc2_ = this.si.join(",");
            ExternalInterface.call("showPopupSEHelpSocialItem",this.eBuilding.buildingReference.tx,this.eBuilding.buildingReference.ty,Base.Main.townID,this.eBuilding.iID,_loc2_,this.eBuilding.sName);
         }
      }
      
"""
    social.write_text(
        social_text[:old_social_start] + social_method + social_text[old_social_end:]
    )
    replace_once(
        social,
        """         else
         {
            if(_loc2_)
            {
               Base.Iso.dispatchEvent(new IsoElementEvent(IsoEngine.SOCIAL_BUILDING_OPENED,this.eBuilding));""",
        """         else
         {
            this.eBuilding.buildingReference.loaded.attrs.staffRoster = this.si.concat();
            this.eBuilding.buildingReference.loaded.attrs.staffRoles = this.arWorkerNames.concat();
            if(this.eBuilding.buildingReference.building.subcat_functional == Constants.SUBCATFUNC_BUILDING_EAGLE)
            {
               Base.Iso.eEagles = this.eBuilding;
            }
            if(_loc2_)
            {
               Base.Iso.dispatchEvent(new IsoElementEvent(IsoEngine.SOCIAL_BUILDING_OPENED,this.eBuilding));""",
    )
    replace_once(
        iso_engine,
        """                     case Constants.SUBCATFUNC_BUILDING_EAGLE:
                        this.eEagles = IsoBuilding(param1);""",
        """                     case Constants.SUBCATFUNC_BUILDING_EAGLE:
                        if(!Base.Main.IsBeingBuilt(param1))
                        {
                           this.eEagles = IsoBuilding(param1);
                        }""",
    )

    replace_once(
        quest_finished,
        """         savingResults.visible = true;
         Quest.sendResults();""",
        """         savingResults.visible = true;
         Quest.sendResults();
         savingResults.visible = false;""",
    )
    replace_once(
        quest_finished,
        "   public class PopupQuestFinished extends PopupQuestFinishedMC\n   {\n",
        """   public class PopupQuestFinished extends PopupQuestFinishedMC
   {
      
      private static const GAMEPLAY_BEHAVIOR_AUDIT:String = "socialemperors-gameplay-behaviors-v26";
""",
    )

    replace_once(
        popup_daily,
        """      private static const GAMEPLAY_LIVE_DAILY_FIX:String = "socialemperors-live-daily-v1";
""",
        """      private static const GAMEPLAY_LIVE_DAILY_FIX:String = "socialemperors-live-daily-v1";
      
      private static const DAILY_CLAIM_TIMESTAMP_FIX:String = "socialemperors-daily-claim-timestamp-v1";
""",
    )
    replace_once(
        popup_daily,
        "Base.Player.privateState.timestampLastBonus = Base.Main.clientTimestamp();",
        "Base.Player.privateState.timestampLastBonus = Math.max(Base.Main.clientTimestamp(),Base.Main.lastServerTimestamp);",
    )

    replace_once(
        projectiles,
        "   public class Projectiles extends Sprite\n   {\n",
        """   public class Projectiles extends Sprite
   {
      
      private static const MINI_FIREBALL_ASSET_FIX:String = "socialemperors-mini-fireball-asset-v1";
""",
    )
    replace_once(
        projectiles,
        """         var _loc5_:Class = Class(_loc4_.loaderInfo.applicationDomain.getDefinition(_loc3_));
""",
        """         var _loc5_:Class = null;
         if(_loc3_ == "p.miniFireball2" && !_loc4_.loaderInfo.applicationDomain.hasDefinition(_loc3_))
         {
            _loc5_ = Class(_loc4_.loaderInfo.applicationDomain.getDefinition("p.fireBall_02"));
         }
         else
         {
            _loc5_ = Class(_loc4_.loaderInfo.applicationDomain.getDefinition(_loc3_));
         }
""",
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-behavior-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", CLASSES,
            "-export", "script", str(export_dir), str(work_swf),
        ], check=True)
        scripts = export_dir / "scripts"
        patch_sources(scripts)
        # Import one modified class at a time. FFDec otherwise recompiles all
        # selected decompilations as one batch, including unchanged helper
        # expressions that its own importer cannot always round-trip.
        for relative in SCRIPT_FILES:
            one = tmp / "one-script"
            if one.exists():
                shutil.rmtree(one)
            target = one / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scripts / relative, target)
            subprocess.run([
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
                "-importScript", str(work_swf), str(work_swf), str(one),
            ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)
    raw = output.read_bytes()
    if raw[:3] == b"CWS":
        raw = b"FWS" + raw[3:8] + zlib.decompress(raw[8:])
    marker = b"socialemperors-gameplay-behaviors-v26"
    if raw.count(marker) != 1:
        raise RuntimeError("patched SWF release marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
