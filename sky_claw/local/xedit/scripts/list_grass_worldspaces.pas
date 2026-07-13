{
  list_grass_worldspaces.pas — xEdit Pascal script: worldspaces que generan pasto.

  Réplica del script comunitario "Worldspaces with Grass" para el pipeline de
  Pre-Cache Grass (NGIO, Stage 8 del SOP): la lista resultante alimenta la
  variable OnlyPregenerateWorldSpaces de GrassControl.ini — precachear solo
  donde hay pasto recorta horas de proceso.

  Algoritmo: un worldspace "tiene pasto" si algún LAND de sus celdas usa una
  landscape texture (LTEX) cuya version GANADORA define un Grass List (GNAM
  con entradas GRAS). Se consideran las capas BTXT/ATXT y el legacy VTEX.
  WinningOverride en LTEX/LAND/WRLD: lo que el motor (y NGIO) realmente ve.

  Output (stdout via AddMessage, pipe-delimited — '|' es invalido en filenames
  de Windows; consumido por grass_analyzer.py, anclado por
  tests/test_grass_scripts_sync.py):

    WSGRASS|<FormID 8hex>|<EditorID del WRLD ganador>|<Plugin master del WRLD>

  Linea final (obligatoria — su ausencia = scan truncado; land_scanned=0 con
  load order cargado delata un scan roto):

    SUMMARY|grass_worldspaces=<N>|land_scanned=<M>|ltex_grass=<K>

  NO se filtran test-worlds: el script reporta hechos; el filtro es de las
  capas superiores que arman el INI.

  Este script es READ-ONLY — jamas modifica un plugin cargado.
}
unit list_grass_worldspaces;

var
  slLtexCache: TStringList;   { 'FORMIDhex=1|0' — ¿la LTEX ganadora tiene GNAM? }
  slWorlds: TStringList;      { ordenada, 'FORMIDhex=EditorID~Plugin' — dedup por WRLD }
  landScanned, ltexGrass: Integer;

function Initialize: Integer;
begin
  slLtexCache := TStringList.Create;
  slLtexCache.Sorted := True;
  slWorlds := TStringList.Create;
  slWorlds.Sorted := True;
  landScanned := 0;
  ltexGrass := 0;
  Result := 0;
end;

{ ¿La version ganadora de la LTEX define un Grass List con entradas? }
function LtexHasGrass(ltex: IInterface): Boolean;
var
  win, gnam: IInterface;
begin
  Result := False;
  win := WinningOverride(ltex);
  if not ElementExists(win, 'GNAM') then
    Exit;
  gnam := ElementBySignature(win, 'GNAM');
  Result := Assigned(gnam);
end;

{ Cache por FormID: los mismos LTEX se repiten en miles de LAND. }
function CachedLtexHasGrass(ltex: IInterface): Boolean;
var
  key: string;
  idx: Integer;
begin
  key := IntToHex(FormID(ltex), 8);
  idx := slLtexCache.IndexOfName(key);
  if idx >= 0 then begin
    Result := slLtexCache.ValueFromIndex[idx] = '1';
    Exit;
  end;
  Result := LtexHasGrass(ltex);
  if Result then begin
    slLtexCache.Add(key + '=1');
    Inc(ltexGrass);
  end else
    slLtexCache.Add(key + '=0');
end;

{ ¿Alguna capa de textura del LAND (BTXT/ATXT/VTEX legacy) tiene pasto? }
function LandHasGrass(land: IInterface): Boolean;
var
  layers, layer, tex: IInterface;
  i: Integer;
begin
  Result := False;
  layers := ElementByName(land, 'Layers');
  if Assigned(layers) then
    for i := 0 to ElementCount(layers) - 1 do begin
      layer := ElementByIndex(layers, i);
      tex := LinksTo(ElementByPath(layer, 'BTXT\Land Texture'));
      if not Assigned(tex) then
        tex := LinksTo(ElementByPath(layer, 'ATXT\Land Texture'));
      if Assigned(tex) and CachedLtexHasGrass(tex) then begin
        Result := True;
        Exit;
      end;
    end;
  { VTEX (legacy): lista plana de referencias a LTEX. }
  layers := ElementBySignature(land, 'VTEX');
  if Assigned(layers) then
    for i := 0 to ElementCount(layers) - 1 do begin
      tex := LinksTo(ElementByIndex(layers, i));
      if Assigned(tex) and CachedLtexHasGrass(tex) then begin
        Result := True;
        Exit;
      end;
    end;
end;

{ WRLD dueño del LAND: LAND vive en el grupo Temporary de su CELL exterior;
  se sube por contenedores hasta el grupo World Children (GroupType 1), cuyo
  contenedor directo es el WRLD. Devuelve nil si no se resuelve (LAND
  huerfano) — el caller lo saltea. }
function WorldspaceOf(land: IInterface): IInterface;
var
  node: IInterface;
begin
  Result := nil;
  node := GetContainer(land);
  while Assigned(node) do begin
    if (ElementType(node) = etGroupRecord) and (GroupType(node) = 1) then begin
      Result := GetContainer(node);
      Exit;
    end;
    node := GetContainer(node);
  end;
end;

function Process(e: IInterface): Integer;
var
  wrld, winWrld: IInterface;
  key: string;
begin
  Result := 0;
  if Signature(e) <> 'LAND' then
    Exit;
  { Una vez por record logico (mismo criterio que list_all_conflicts.pas). }
  if not IsMaster(e) then
    Exit;
  Inc(landScanned);

  wrld := WorldspaceOf(e);
  if not Assigned(wrld) then
    Exit;

  key := IntToHex(FormID(wrld), 8);
  { Short-circuit clave de perf: Tamriel queda confirmado tras pocos LAND y
    los ~30k restantes cortan aca sin escanear capas. }
  if slWorlds.IndexOfName(key) >= 0 then
    Exit;

  if not LandHasGrass(WinningOverride(e)) then
    Exit;

  winWrld := WinningOverride(wrld);
  { '~' como separador interno del value: '|' se reserva para el output y
    '=' lo usa el name=value del TStringList. }
  slWorlds.Add(key + '=' + EditorID(winWrld) + '~' + GetFileName(GetFile(MasterOrSelf(wrld))));
end;

function Finalize: Integer;
var
  i, sep: Integer;
  value: string;
begin
  { Emision diferida y ordenada por FormID: output deterministico. }
  for i := 0 to slWorlds.Count - 1 do begin
    value := slWorlds.ValueFromIndex[i];
    sep := Pos('~', value);
    AddMessage('WSGRASS|' + slWorlds.Names[i] + '|' +
               Copy(value, 1, sep - 1) + '|' +
               Copy(value, sep + 1, Length(value)));
  end;
  AddMessage('SUMMARY|grass_worldspaces=' + IntToStr(slWorlds.Count) +
             '|land_scanned=' + IntToStr(landScanned) +
             '|ltex_grass=' + IntToStr(ltexGrass));
  slLtexCache.Free;
  slWorlds.Free;
  Result := 0;
end;

end.
