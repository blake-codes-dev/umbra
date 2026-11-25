-- name: fullbright
-- desc: removes darkness without touching your other lighting settings
local Lighting = game:GetService("Lighting")

if _G.__umbraBright then
    for prop, value in pairs(_G.__umbraBright) do Lighting[prop] = value end
    _G.__umbraBright = nil
else
    _G.__umbraBright = {
        Ambient = Lighting.Ambient,
        OutdoorAmbient = Lighting.OutdoorAmbient,
        Brightness = Lighting.Brightness,
        FogEnd = Lighting.FogEnd,
    }
    Lighting.Ambient = Color3.fromRGB(180, 180, 180)
    Lighting.OutdoorAmbient = Color3.fromRGB(180, 180, 180)
    Lighting.Brightness = 3
    Lighting.FogEnd = 1e6
end
