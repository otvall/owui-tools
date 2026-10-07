def _build_tool_data_bridge(result: Any, title: str) -> str:
    data_json = _safe_json_for_html(result)

    return (
        '<script id="iv-tool-data" type="application/json">'
        f"{data_json}</script>"

        '<script id="iv-tool-title" type="text/plain">'
        f"{title}</script>"

        "<script>(function(){"

        # --------------------------------------------------
        # getToolData
        # --------------------------------------------------

        "function read(id){"
            "var el=document.getElementById(id);"
            "if(!el)return null;"
            "try{return JSON.parse(el.textContent);}"
            "catch(e){return null;}"
        "}"

        "function readText(id){"
            "var el=document.getElementById(id);"
            "return el ? el.textContent : null;"
        "}"

        "var data=read('iv-tool-data');"
        "var title=readText('iv-tool-title');"

        "window.getToolData=function(){"
            "return data;"
        "};"

        # --------------------------------------------------
        # Registry
        # --------------------------------------------------

        "try{"

            "var host=window.parent;"

            "if(host && host!==window){"

                # ------------------------------------------
                # Создаем registry один раз
                # ------------------------------------------

                "if(!host.__OWUI_VIZ_REGISTRY__){"

                    # metadata
                    "var items=new Map();"

                    # toolDataId -> getToolData()
                    "var getters=new Map();"

                    "host.__OWUI_VIZ_REGISTRY__={"

                        "register:function(entry,getter){"
                            "items.set(entry.toolDataId,entry);"
                            "getters.set(entry.toolDataId,getter);"
                        "},"

                        "list:function(){"
                            "return Array.from(items.values());"
                        "},"

                        "getData:function(toolDataId){"

                            "var getter=getters.get(toolDataId);"

                            "if(!getter){"
                                "throw new Error("
                                    "'getToolData not found: '+toolDataId"
                                ");"
                            "}"

                            "return getter();"
                        "}"

                    "};"
                "}"

                # ------------------------------------------
                # messageId
                # ------------------------------------------

                "var frame=window.frameElement;"

                "var msgEl="
                    "frame && "
                    "frame.closest && "
                    "frame.closest('[id^=\"message-\"]');"

                "var messageId=null;"

                "if(msgEl && msgEl.id){"
                    "messageId=msgEl.id.replace(/^message-/,'');"
                "}"

                # ------------------------------------------
                # Уникальный ID именно этого getToolData
                # ------------------------------------------

                "var toolDataId;"

                "if("
                    "window.crypto && "
                    "typeof window.crypto.randomUUID==='function'"
                "){"
                    "toolDataId=window.crypto.randomUUID();"
                "}else{"
                    "toolDataId="
                        "'tool-data-'+"
                        "Date.now().toString(36)+'-'+"
                        "Math.random().toString(36).slice(2);"
                "}"

                # можно сохранить и внутри iframe
                "window.toolDataId=toolDataId;"

                # ------------------------------------------
                # Register
                # ------------------------------------------

                "host.__OWUI_VIZ_REGISTRY__.register("

                    "{"
                        "title:title,"
                        "messageId:messageId,"
                        "toolDataId:toolDataId"
                    "},"

                    "function(){"
                        "return window.getToolData();"
                    "}"

                ");"

                "console.debug("
                    "'[viz-registry] registered',"
                    "{"
                        "title:title,"
                        "messageId:messageId,"
                        "toolDataId:toolDataId"
                    "}"
                ");"

            "}"

        "}catch(e){"
            "console.error('[viz-registry]',e);"
        "}"

        "})();</script>"
    )
