-- name: unlock fps
-- desc: lifts the frame cap. menu reads fps back in the status line
if setfpscap then
    setfpscap(240)
    print("fps cap lifted to 240")
else
    print("setfpscap missing: this needs the executor, not a LocalScript")
end
